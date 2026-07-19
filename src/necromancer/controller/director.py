"""Deterministic repair loop; a Surgeon proposes, evidence decides."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Literal, Mapping, Protocol

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
from necromancer.repair.patch_policy import PatchPolicyConfig, evaluate_patch


DirectorStatus = Literal[
    "accepted",
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


class Surgeon(Protocol):
    def propose(self, evaluation: int, best_score: Score) -> PatchProposal | None:
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

    def propose(self, evaluation: int, best_score: Score) -> PatchProposal | None:
        index = evaluation - 1
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
class DirectorResult:
    status: DirectorStatus
    baseline_score: Score
    final_score: Score
    accepted_snapshot_id: str | None
    evaluations: int
    events: tuple[DirectorEvent, ...]
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
                    started,
                )
            proposal = surgeon.propose(evaluation, best_score)
            if proposal is None:
                if staged_candidate is not None:
                    workspace.discard(staged_candidate)
                return self._result(
                    "accepted"
                    if accepted_snapshot_id
                    else "baseline_not_runnable"
                    if isinstance(best_score, BootstrapScore)
                    else "surgeon_exhausted",
                    baseline_score,
                    best_score,
                    accepted_snapshot_id,
                    evaluation - 1,
                    events,
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
                    started,
                )

        if staged_candidate is not None:
            workspace.discard(staged_candidate)
        return self._result(
            "accepted" if accepted_snapshot_id else "max_evaluations_reached",
            baseline_score,
            best_score,
            accepted_snapshot_id,
            self.config.max_evaluations,
            events,
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
        started: float,
    ) -> DirectorResult:
        return DirectorResult(
            status=status,
            baseline_score=baseline_score,
            final_score=final_score,
            accepted_snapshot_id=accepted_snapshot_id,
            evaluations=evaluations,
            events=tuple(events),
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


def _deadline_expired(started: float, deadline_seconds: float) -> bool:
    return time.monotonic() - started >= deadline_seconds
