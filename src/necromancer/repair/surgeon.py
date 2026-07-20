"""Model-backed patch proposal generation for the deterministic Director."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol

from pydantic import Field

from necromancer.controller.director import PatchProposal, SurgeonContext
from necromancer.domain.models import StrictModel
from necromancer.llm.client import OpenAIResponsesClient
from necromancer.repair.patch_policy import is_protected_path


class StructuredLLMClient(Protocol):
    def generate(
        self,
        *,
        system_prompt: str,
        user_content: str,
        response_model: type[StrictModel],
    ) -> StrictModel: ...


class SurgeonPatch(StrictModel):
    """The single-file patch contract emitted by GPT and validated locally."""

    plan_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    expected_affected_tests: list[str] = Field(min_length=1)
    target_file: str = Field(min_length=1)
    preimage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diff: str = Field(min_length=1)


@dataclass
class RealSurgeon:
    """Ask GPT for one patch against the current accepted snapshot."""

    client: StructuredLLMClient = field(default_factory=OpenAIResponsesClient)
    system_prompt: str = field(default_factory=lambda: _prompt_path().read_text(encoding="utf-8"))
    generated_patches: list[SurgeonPatch] = field(default_factory=list, init=False)

    def propose(self, context: SurgeonContext) -> PatchProposal | None:
        target = _earliest_source_target(context.repository_path, context.result_path)
        if target is None:
            return None
        target_path, evidence = target
        target_contents = target_path.read_text(encoding="utf-8", errors="replace")
        target_file = target_path.relative_to(context.repository_path).as_posix()
        preimage_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
        request = _request_content(
            context,
            target_file=target_file,
            preimage_sha256=preimage_sha256,
            target_contents=target_contents,
            evidence=evidence,
        )
        response = self.client.generate(
            system_prompt=self.system_prompt,
            user_content=request,
            response_model=SurgeonPatch,
        )
        if not isinstance(response, SurgeonPatch):
            raise TypeError("Structured LLM client returned an unexpected response model")
        self.generated_patches.append(response)
        return PatchProposal(
            diff=response.diff,
            preimage_sha256={response.target_file: response.preimage_sha256},
            description=f"{response.plan_id}: {response.rationale}",
            plan_id=context.retry_plan_id or response.plan_id,
        )


def _prompt_path() -> Path:
    return Path(__file__).resolve().parents[1] / "llm" / "prompts" / "surgeon.md"


def _request_content(
    context: SurgeonContext,
    *,
    target_file: str,
    preimage_sha256: str,
    target_contents: str,
    evidence: str,
) -> str:
    result = json.loads(context.result_path.read_text(encoding="utf-8"))
    retry_instruction = (
        (
            "This is formatting repair attempt "
            f"{context.apply_retry_number} for the same plan item "
            f"{context.retry_plan_id}. Return a corrected patch for that item; "
            "do not change scope or touch protected tests/configuration.\n"
            "Exact previous rejection:\n"
            f"{context.apply_rejection_feedback}"
        )
        if context.apply_rejection_feedback is not None
        else None
    )
    parts = [
        f"Evaluation: {context.evaluation}",
        f"Current score: {context.best_score.score}",
        "Earliest blocking traceback:\n" + evidence,
        "result.json:\n" + json.dumps(result, indent=2, sort_keys=True),
        f"Target file: {target_file}",
        f"Target preimage SHA-256: {preimage_sha256}",
        "Target file contents:\n" + target_contents,
    ]
    if retry_instruction is not None:
        parts.append(retry_instruction)
    return "\n\n".join(parts)


def _earliest_source_target(repository: Path, result_path: Path) -> tuple[Path, str] | None:
    """Pick the innermost non-test Python frame from the earliest failed report."""

    result = json.loads(result_path.read_text(encoding="utf-8"))
    for report in _failed_reports(result):
        traceback = report.get("longrepr")
        if not isinstance(traceback, str):
            continue
        for candidate in reversed(_traceback_files(traceback, repository)):
            relative = candidate.relative_to(repository).as_posix()
            if not is_protected_path(relative):
                return candidate, traceback
    return None


def _failed_reports(result: dict[str, Any]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for phase_name in ("collection", "test"):
        phase = result.get(phase_name)
        if not isinstance(phase, dict):
            continue
        for key in ("collection_reports", "test_reports"):
            values = phase.get(key)
            if not isinstance(values, list):
                continue
            reports.extend(
                value
                for value in values
                if isinstance(value, dict) and value.get("outcome") in {"failed", "error"}
            )
    return reports


def _traceback_files(traceback: str, repository: Path) -> list[Path]:
    files: list[Path] = []
    for raw_path in re.findall(r"^(.+?\.py):\d+: in ", traceback, flags=re.MULTILINE):
        candidate = _path_in_repository(raw_path, repository)
        if candidate is not None:
            files.append(candidate)
    return files


def _path_in_repository(raw_path: str, repository: Path) -> Path | None:
    normalised = raw_path.replace("\\", "/")
    candidate = Path(normalised)
    if not candidate.is_absolute():
        candidate = repository / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return (
        resolved
        if resolved.is_relative_to(repository.resolve()) and resolved.is_file()
        else None
    )
