"""Apply policy-approved unified diffs only to disposable candidates."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Literal, Mapping

from necromancer.execution.workspace import CandidateSnapshot
from necromancer.repair.patch_policy import PatchDecision, PatchPolicyConfig, evaluate_patch


@dataclass(frozen=True)
class PatchApplyResult:
    status: Literal["applied", "rejected"]
    reason: str | None
    decision: PatchDecision
    stdout: str = ""
    stderr: str = ""


def apply_patch_to_candidate(
    candidate: CandidateSnapshot,
    diff: str,
    *,
    preimage_sha256: Mapping[str, str],
    high_risk: bool = False,
    config: PatchPolicyConfig | None = None,
) -> PatchApplyResult:
    """Policy-check, then apply a diff using ``git apply --check`` and ``git apply``.

    Rejection or a failed check removes only the candidate directory.  Accepted
    workspace snapshots are never passed to this function and are not mutated.
    """

    if candidate.path.resolve().parent.name != "candidates":
        decision = PatchDecision(False, ("candidate is outside a candidates directory",), (), 0)
        return PatchApplyResult("rejected", decision.reasons[0], decision)

    decision = evaluate_patch(
        diff,
        candidate.path,
        preimage_sha256=preimage_sha256,
        high_risk=high_risk,
        config=config,
    )
    if not decision.allowed:
        _discard_candidate_directory(candidate.path)
        return PatchApplyResult("rejected", "; ".join(decision.reasons), decision)

    check = _git_apply(candidate.path, diff, check=True)
    if check.returncode != 0:
        _discard_candidate_directory(candidate.path)
        return PatchApplyResult(
            "rejected",
            "git apply --check rejected the diff",
            decision,
            check.stdout,
            check.stderr,
        )
    applied = _git_apply(candidate.path, diff, check=False)
    if applied.returncode != 0:
        _discard_candidate_directory(candidate.path)
        return PatchApplyResult(
            "rejected",
            "git apply rejected the diff after a successful check",
            decision,
            applied.stdout,
            applied.stderr,
        )
    return PatchApplyResult("applied", None, decision, applied.stdout, applied.stderr)


def _git_apply(candidate_path: Path, diff: str, *, check: bool) -> subprocess.CompletedProcess[str]:
    command = ["git", "apply"]
    if check:
        command.append("--check")
    command.append("-")
    return subprocess.run(
        command,
        cwd=candidate_path,
        input=diff,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            # Candidates are commonly stored below an ignored directory in the
            # controller repository. Stop Git from discovering that outer repo
            # and silently skipping this disposable patch as an ignored path.
            "GIT_CEILING_DIRECTORIES": str(candidate_path.parent),
        },
    )


def _discard_candidate_directory(candidate_path: Path) -> None:
    if candidate_path.exists():
        shutil.rmtree(candidate_path)
