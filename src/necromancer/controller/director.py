"""Deterministic repair loop; a Surgeon proposes, evidence decides."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Literal, Mapping, Protocol

from necromancer.execution.pytest_runner import PytestRunner
from necromancer.execution.scoring import (
    BootstrapScore,
    CollectionFingerprint,
    Score,
    TestScore,
    accepts_test_candidate,
    collection_frontier_advance,
    compute_score,
)
from necromancer.execution.workspace import Workspace
from necromancer.repair.patch_apply import apply_patch_to_candidate
from necromancer.repair.patch_policy import (
    PatchPolicyConfig,
    evaluate_patch,
    is_protected_path,
)


DirectorStatus = Literal[
    "full_revival",
    "partial_revival",
    "baseline_not_runnable",
    "surgeon_exhausted",
    "max_evaluations_reached",
    "deadline_reached",
    "policy_rejected",
    "apply_rejected",
    "score_rejected",
]


@dataclass(frozen=True)
class PatchProposal:
    diff: str
    preimage_sha256: Mapping[str, str]
    high_risk: bool = False
    description: str = ""


@dataclass(frozen=True)
class SurgeonContext:
    """Current accepted-state evidence made available to a patch proposer."""

    evaluation: int
    best_score: Score
    repository_path: Path
    result_path: Path


class Surgeon(Protocol):
    def propose(self, context: SurgeonContext) -> PatchProposal | None:
        """Return one patch proposal, or ``None`` when no work remains."""


@dataclass(frozen=True)
class FixtureSurgeon:
    """Fixture-backed Surgeon replacement used for deterministic end-to-end tests."""

    proposals: tuple[PatchProposal, ...]

    @classmethod
    def from_fixture(cls, fixture_path: Path | str) -> "FixtureSurgeon":
        return cls.from_fixtures(fixture_path)

    @classmethod
    def from_fixtures(
        cls, *fixture_paths: Path | str
    ) -> "FixtureSurgeon":
        proposals = []
        for fixture_path in fixture_paths:
            document = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
            proposals.append(
                PatchProposal(
                    diff=str(document["diff"]),
                    preimage_sha256=dict(document["preimage_sha256"]),
                    high_risk=bool(document.get("high_risk", False)),
                    description=str(document.get("description", "")),
                )
            )
        return cls(tuple(proposals))

    def propose(self, context: SurgeonContext) -> PatchProposal | None:
        index = context.evaluation - 1
        return self.proposals[index] if 0 <= index < len(self.proposals) else None


@dataclass(frozen=True)
class DirectorConfig:
    max_evaluations: int = 15
    global_deadline_seconds: float = 20 * 60
    stop_after_rejection: bool = True
    # A staged bundle remains disposable until one of its patches produces
    # strict progress against the accepted baseline. The default preserves the
    # original one-patch-at-a-time behavior.
    max_staged_patches: int = 1
    patch_policy: PatchPolicyConfig | None = None


@dataclass(frozen=True)
class DirectorEvent:
    evaluation: int
    status: Literal[
        "baseline",
        "policy_rejected",
        "apply_rejected",
        "accepted",
        "score_rejected",
        "bootstrap_accepted",
        "staged",
    ]
    reason: str | None
    before_score: tuple[int | float, ...] | None
    after_score: tuple[int | float, ...] | None
    patch_description: str = ""


@dataclass(frozen=True)
class ReviewRecord:
    """A deterministic, evidence-linked request for human review."""

    nodeid: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class DirectorResult:
    status: DirectorStatus
    baseline_score: Score
    final_score: Score
    accepted_snapshot_id: str | None
    evaluations: int
    events: tuple[DirectorEvent, ...]
    review_records: tuple[ReviewRecord, ...]
    elapsed_seconds: float


class Director:
    """Run bounded candidate evaluations and promote only evidence-backed work."""

    def __init__(
        self,
        runner: PytestRunner | None = None,
        config: DirectorConfig | None = None,
    ) -> None:
        self.runner = runner or PytestRunner()
        self.config = config or DirectorConfig()

    def revive(self, workspace: Workspace, surgeon: Surgeon) -> DirectorResult:
        started = time.monotonic()
        events: list[DirectorEvent] = []
        baseline_candidate = workspace.create_candidate("baseline")
        baseline_run = self.runner.run(
            baseline_candidate.path, baseline_candidate.artifact_dir / "baseline"
        )
        baseline_score = compute_score(baseline_run.result_path)
        best_score = baseline_score
        best_result_path = baseline_run.result_path
        best_repository_path = baseline_candidate.path
        events.append(
            DirectorEvent(
                evaluation=0,
                status="baseline",
                reason=None,
                before_score=None,
                after_score=_score_tuple(best_score),
            )
        )
        seen_fingerprints: set[CollectionFingerprint] = (
            set(best_score.collection_error_fingerprints)
            if isinstance(best_score, BootstrapScore)
            else set()
        )
        accepted_snapshot_id: str | None = None
        staged_candidate = None
        staged_patch_count = 0
        for evaluation in range(1, self.config.max_evaluations + 1):
            if _deadline_expired(started, self.config.global_deadline_seconds):
                if staged_candidate is not None:
                    workspace.discard(staged_candidate)
                return self._result(
                    "deadline_reached",
                    baseline_score,
                    best_score,
                    accepted_snapshot_id,
                    evaluation - 1,
                    events,
                    best_result_path,
                    best_repository_path,
                    started,
                )
            proposal = surgeon.propose(
                SurgeonContext(
                    evaluation=evaluation,
                    best_score=best_score,
                    repository_path=best_repository_path,
                    result_path=best_result_path,
                )
            )
            if proposal is None:
                if staged_candidate is not None:
                    workspace.discard(staged_candidate)
                return self._result(
                    "full_revival"
                    if _is_full_revival(best_score)
                    else "partial_revival"
                    if accepted_snapshot_id and isinstance(best_score, TestScore)
                    else "baseline_not_runnable"
                    if isinstance(best_score, BootstrapScore)
                    else "surgeon_exhausted",
                    baseline_score,
                    best_score,
                    accepted_snapshot_id,
                    evaluation - 1,
                    events,
                    best_result_path,
                    best_repository_path,
                    started,
                )

            candidate = staged_candidate or workspace.create_candidate(
                f"evaluation-{evaluation:04d}"
            )
            decision = evaluate_patch(
                proposal.diff,
                candidate.path,
                preimage_sha256=proposal.preimage_sha256,
                high_risk=proposal.high_risk,
                config=self.config.patch_policy,
            )
            if not decision.allowed:
                workspace.discard(candidate)
                if candidate is staged_candidate:
                    staged_candidate = None
                    staged_patch_count = 0
                events.append(
                    DirectorEvent(
                        evaluation,
                        "policy_rejected",
                        "; ".join(decision.reasons),
                        _score_tuple(best_score),
                        None,
                        proposal.description,
                    )
                )
                if self.config.stop_after_rejection:
                    return self._result(
                        "policy_rejected",
                        baseline_score,
                        best_score,
                        accepted_snapshot_id,
                        evaluation,
                        events,
                        best_result_path,
                        best_repository_path,
                        started,
                    )
                continue

            applied = apply_patch_to_candidate(
                candidate,
                proposal.diff,
                preimage_sha256=proposal.preimage_sha256,
                high_risk=proposal.high_risk,
                config=self.config.patch_policy,
            )
            if applied.status != "applied":
                workspace.discard(candidate)
                if candidate is staged_candidate:
                    staged_candidate = None
                    staged_patch_count = 0
                events.append(
                    DirectorEvent(
                        evaluation,
                        "apply_rejected",
                        applied.reason,
                        _score_tuple(best_score),
                        None,
                        proposal.description,
                    )
                )
                return self._result(
                    "apply_rejected",
                    baseline_score,
                    best_score,
                    accepted_snapshot_id,
                    evaluation,
                    events,
                    best_result_path,
                    best_repository_path,
                    started,
                )

            candidate_run = self.runner.run(
                candidate.path, candidate.artifact_dir / f"evaluation-{evaluation:04d}"
            )
            candidate_score = compute_score(
                candidate_run.result_path,
                baseline=best_score if isinstance(best_score, TestScore) else None,
            )
            accepted, bootstrap_advance = _accepts_candidate(
                best_score, candidate_score, seen_fingerprints
            )
            if accepted:
                accepted_snapshot_id = workspace.accept(candidate)
                events.append(
                    DirectorEvent(
                        evaluation,
                        "bootstrap_accepted" if bootstrap_advance else "accepted",
                        None,
                        _score_tuple(best_score),
                        _score_tuple(candidate_score),
                        proposal.description,
                    )
                )
                if isinstance(candidate_score, BootstrapScore):
                    seen_fingerprints.update(candidate_score.collection_error_fingerprints)
                best_score = candidate_score
                best_result_path = candidate_run.result_path
                best_repository_path = candidate.path
                staged_candidate = None
                staged_patch_count = 0
                continue

            if (
                self.config.max_staged_patches > 1
                and staged_patch_count + 1 < self.config.max_staged_patches
            ):
                staged_candidate = candidate
                staged_patch_count += 1
                events.append(
                    DirectorEvent(
                        evaluation,
                        "staged",
                        "candidate is retained only for the bounded staged patch bundle",
                        _score_tuple(best_score),
                        _score_tuple(candidate_score),
                        proposal.description,
                    )
                )
                continue

            workspace.discard(candidate)
            staged_candidate = None
            events.append(
                DirectorEvent(
                    evaluation,
                    "score_rejected",
                    "candidate did not satisfy deterministic progress rules",
                    _score_tuple(best_score),
                    _score_tuple(candidate_score),
                    proposal.description,
                )
            )
            if self.config.stop_after_rejection:
                return self._result(
                    "score_rejected",
                    baseline_score,
                    best_score,
                    accepted_snapshot_id,
                    evaluation,
                    events,
                    best_result_path,
                    best_repository_path,
                    started,
                )

        if staged_candidate is not None:
            workspace.discard(staged_candidate)
        return self._result(
            "full_revival"
            if _is_full_revival(best_score)
            else "partial_revival"
            if accepted_snapshot_id and isinstance(best_score, TestScore)
            else "max_evaluations_reached",
            baseline_score,
            best_score,
            accepted_snapshot_id,
            self.config.max_evaluations,
            events,
            best_result_path,
            best_repository_path,
            started,
        )

    def _result(
        self,
        status: DirectorStatus,
        baseline_score: Score,
        final_score: Score,
        accepted_snapshot_id: str | None,
        evaluations: int,
        events: list[DirectorEvent],
        final_result_path: Path,
        final_repository_path: Path,
        started: float,
    ) -> DirectorResult:
        review_records = (
            _review_records(final_result_path, final_repository_path, final_score)
            if status == "partial_revival"
            else ()
        )
        return DirectorResult(
            status=status,
            baseline_score=baseline_score,
            final_score=final_score,
            accepted_snapshot_id=accepted_snapshot_id,
            evaluations=evaluations,
            events=tuple(events),
            review_records=review_records,
            elapsed_seconds=round(time.monotonic() - started, 6),
        )


def _accepts_candidate(
    best: Score,
    candidate: Score,
    seen_fingerprints: set[CollectionFingerprint],
) -> tuple[bool, bool]:
    if isinstance(best, TestScore) and isinstance(candidate, TestScore):
        return accepts_test_candidate(best, candidate), False
    if isinstance(best, BootstrapScore) and isinstance(candidate, BootstrapScore):
        return (
            candidate.score > best.score
            or collection_frontier_advance(
                best, candidate, seen_fingerprints=seen_fingerprints
            ),
            True,
        )
    if isinstance(best, BootstrapScore) and isinstance(candidate, TestScore):
        return math.isfinite(candidate.debt), True
    return False, False


def _score_tuple(score: Score) -> tuple[int | float, ...]:
    return score.score


def _is_full_revival(score: Score) -> bool:
    return (
        isinstance(score, TestScore)
        and score.collection_complete
        and score.debt == 0
        and bool(score.node_outcomes)
        and all(outcome == "passed" for outcome in score.node_outcomes.values())
    )


def _review_records(
    result_path: Path, repository_path: Path, score: Score
) -> tuple[ReviewRecord, ...]:
    if not isinstance(score, TestScore):
        return ()
    result = _read_json(result_path)
    test = _mapping(result.get("test"))
    reports_by_node = _reports_by_node(test)
    records: list[ReviewRecord] = []
    for nodeid, outcome in sorted(score.node_outcomes.items()):
        if outcome == "passed":
            continue
        location = _report_location(reports_by_node.get(nodeid, ()))
        if location is not None and is_protected_path(location):
            evidence = _python2_command_evidence(repository_path, location, nodeid)
            if evidence is not None:
                records.append(
                    ReviewRecord(
                        nodeid=nodeid,
                        reason=(
                            "protected test file contains Python-2 syntax in the "
                            "failing command; editing it is forbidden by patch policy"
                        ),
                        evidence=evidence,
                    )
                )
                continue
            records.append(
                ReviewRecord(
                    nodeid=nodeid,
                    reason=(
                        "failure is located in a protected test file; no "
                        "source-only cause attribution is established"
                    ),
                    evidence=f"captured test location: {location}",
                )
            )
            continue
        records.append(
            ReviewRecord(
                nodeid=nodeid,
                reason="unresolved failure; no deterministic policy attribution is established",
                evidence=(
                    f"captured test location: {location}"
                    if location is not None
                    else "no captured test source location"
                ),
            )
        )
    return tuple(records)


def _reports_by_node(test: dict[str, Any]) -> dict[str, tuple[dict[str, Any], ...]]:
    reports: dict[str, list[dict[str, Any]]] = {}
    raw_reports = test.get("test_reports")
    if not isinstance(raw_reports, list):
        return {}
    for report in raw_reports:
        if not isinstance(report, dict):
            continue
        nodeid = report.get("nodeid")
        if isinstance(nodeid, str):
            reports.setdefault(nodeid, []).append(report)
    return {nodeid: tuple(values) for nodeid, values in reports.items()}


def _report_location(reports: tuple[dict[str, Any], ...]) -> str | None:
    for report in reports:
        if report.get("outcome") not in {"failed", "error"}:
            continue
        location = report.get("location")
        if (
            isinstance(location, list)
            and location
            and isinstance(location[0], str)
        ):
            return location[0]
    return None


def _python2_command_evidence(
    repository_path: Path, relative_path: str, nodeid: str
) -> str | None:
    source_path = _review_source_path(repository_path, relative_path, nodeid)
    if source_path is None:
        return None
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
    except (OSError, SyntaxError):
        return None
    test_name = nodeid.rsplit("::", 1)[-1].split("[", 1)[0]
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name != test_name:
            continue
        start = function.lineno
        end = function.end_lineno or start
        lines = source.splitlines()
        body = "\n".join(lines[start - 1 : end])
        if "python -c" not in body:
            continue
        for line_number, line in enumerate(lines[start - 1 : end], start):
            if re.search(r"\bprint\s+\\?['\"]", line):
                display_path = source_path.relative_to(repository_path)
                return (
                    f"{display_path}:{line_number} contains Python-2 print "
                    "syntax in a python -c command"
                )
    return None


def _review_source_path(
    repository_path: Path, reported_path: str, nodeid: str
) -> Path | None:
    """Resolve evidence only within the accepted candidate's source tree."""

    candidate = (repository_path / reported_path).resolve()
    if candidate.is_relative_to(repository_path.resolve()) and candidate.is_file():
        return candidate
    nodeid_path = Path(nodeid.split("::", 1)[0])
    fallback = (repository_path / nodeid_path).resolve()
    if (
        not nodeid_path.is_absolute()
        and ".." not in nodeid_path.parts
        and fallback.is_relative_to(repository_path.resolve())
        and fallback.is_file()
    ):
        return fallback
    return None


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _deadline_expired(started: float, deadline_seconds: float) -> bool:
    return time.monotonic() - started >= deadline_seconds
