"""Deterministic pre-application policy checks for Surgeon patch proposals."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shlex
from typing import Mapping


DEFAULT_MAX_FILES = 4
DEFAULT_MAX_CHANGED_LINES = 120


@dataclass(frozen=True)
class PatchDecision:
    allowed: bool
    reasons: tuple[str, ...]
    files: tuple[str, ...]
    changed_lines: int


@dataclass(frozen=True)
class PatchPolicyConfig:
    max_files: int = DEFAULT_MAX_FILES
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES


@dataclass(frozen=True)
class _FilePatch:
    old_path: str | None
    new_path: str | None
    lines: tuple[str, ...]

    @property
    def target_path(self) -> str:
        return self.new_path or self.old_path or ""


def evaluate_patch(
    diff: str,
    repository: Path | str,
    *,
    preimage_sha256: Mapping[str, str],
    high_risk: bool = False,
    config: PatchPolicyConfig | None = None,
) -> PatchDecision:
    """Return an allow/reject decision without changing ``repository``."""

    policy = config or PatchPolicyConfig()
    repository_path = Path(repository).resolve()
    reasons: list[str] = []
    try:
        patches = _parse_unified_diff(diff)
    except ValueError as error:
        return PatchDecision(False, (str(error),), (), 0)

    changed_lines = _changed_line_count(patches)
    files = tuple(file_patch.target_path for file_patch in patches)
    if not high_risk:
        if len(files) > policy.max_files:
            reasons.append(
                f"patch changes {len(files)} files; limit is {policy.max_files}"
            )
        if changed_lines > policy.max_changed_lines:
            reasons.append(
                "patch changes "
                f"{changed_lines} lines; limit is {policy.max_changed_lines}"
            )

    for file_patch in patches:
        reasons.extend(_file_patch_reasons(file_patch, repository_path, preimage_sha256))
    reasons.extend(_diff_content_reasons(patches, repository_path))
    return PatchDecision(not reasons, tuple(dict.fromkeys(reasons)), files, changed_lines)


def _parse_unified_diff(diff: str) -> tuple[_FilePatch, ...]:
    if not diff.strip():
        raise ValueError("empty diff")
    lines = diff.splitlines()
    patches: list[_FilePatch] = []
    current_lines: list[str] = []
    old_path: str | None = None
    new_path: str | None = None
    saw_hunk = False

    def finish_current() -> None:
        nonlocal current_lines, old_path, new_path
        if old_path is not None or new_path is not None:
            patches.append(_FilePatch(old_path, new_path, tuple(current_lines)))
        current_lines = []
        old_path = None
        new_path = None

    for line in lines:
        if line.startswith("diff --git "):
            finish_current()
            old_path, new_path = _git_paths(line)
            current_lines.append(line)
            continue
        if line.startswith("--- "):
            if old_path is None and new_path is None:
                old_path = _header_path(line[4:])
            else:
                old_path = _header_path(line[4:])
            current_lines.append(line)
            continue
        if line.startswith("+++ "):
            new_path = _header_path(line[4:])
            current_lines.append(line)
            continue
        if line.startswith("@@ "):
            saw_hunk = True
        current_lines.append(line)
    finish_current()

    if not patches or not saw_hunk:
        raise ValueError("invalid unified diff: no file hunk")
    if any(not patch.target_path for patch in patches):
        raise ValueError("invalid unified diff: missing file path")
    if _changed_line_count(patches) == 0:
        raise ValueError("empty diff: no added or removed lines")
    return tuple(patches)


def _git_paths(line: str) -> tuple[str | None, str | None]:
    try:
        _, _, old_token, new_token = shlex.split(line)
    except ValueError as error:
        raise ValueError(f"invalid diff header: {error}") from error
    return _strip_git_prefix(old_token), _strip_git_prefix(new_token)


def _header_path(value: str) -> str | None:
    token = value.split("\t", 1)[0].strip()
    if token == "/dev/null":
        return None
    return _strip_git_prefix(token)


def _strip_git_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _changed_line_count(patches: tuple[_FilePatch, ...]) -> int:
    return sum(
        1
        for file_patch in patches
        for line in file_patch.lines
        if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
    )


def _file_patch_reasons(
    file_patch: _FilePatch,
    repository: Path,
    preimage_sha256: Mapping[str, str],
) -> list[str]:
    reasons: list[str] = []
    for path in (file_patch.old_path, file_patch.new_path):
        if path is None:
            continue
        reasons.extend(_path_reasons(path))
        if _is_protected_path(path):
            reasons.append(f"protected test or pytest configuration path: {path}")

    if file_patch.old_path is None:
        reasons.append(f"new file creation is not allowed: {file_patch.new_path}")
        return reasons
    if file_patch.new_path is None:
        reasons.append(f"file deletion is not allowed: {file_patch.old_path}")
        return reasons
    if file_patch.old_path != file_patch.new_path:
        reasons.append(
            f"rename or path change is not allowed: {file_patch.old_path} -> {file_patch.new_path}"
        )
        return reasons

    target = repository / file_patch.old_path
    if not target.is_file() or target.is_symlink():
        reasons.append(f"preimage file is missing or not a regular file: {file_patch.old_path}")
        return reasons
    expected_hash = preimage_sha256.get(file_patch.old_path)
    if expected_hash is None:
        reasons.append(f"missing preimage SHA-256 for: {file_patch.old_path}")
        return reasons
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        reasons.append(f"invalid preimage SHA-256 for: {file_patch.old_path}")
        return reasons
    actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual_hash != expected_hash.lower():
        reasons.append(f"preimage SHA-256 mismatch for: {file_patch.old_path}")
    return reasons


def _path_reasons(path: str) -> list[str]:
    path_parts = Path(path).parts
    reasons: list[str] = []
    if Path(path).is_absolute() or ".." in path_parts:
        reasons.append(f"unsafe path: {path}")
    if any(part == ".git" for part in path_parts):
        reasons.append(f"git metadata path is not allowed: {path}")
    return reasons


def _is_protected_path(path: str) -> bool:
    parts = Path(path).parts
    if parts and parts[0] in {"test", "tests"}:
        return True
    return Path(path).name in {"conftest.py", "pytest.ini", "tox.ini"}


def _diff_content_reasons(
    patches: tuple[_FilePatch, ...], repository: Path
) -> list[str]:
    reasons: list[str] = []
    added_lines = [
        line[1:]
        for file_patch in patches
        for line in file_patch.lines
        if line.startswith("+") and not line.startswith("+++")
    ]
    for file_patch in patches:
        lines = file_patch.lines
        if any(
            line.startswith(
                (
                    "GIT binary patch",
                    "Binary files ",
                    "old mode ",
                    "new mode ",
                    "new file mode ",
                    "deleted file mode ",
                    "similarity index ",
                    "rename from ",
                    "rename to ",
                    "copy from ",
                    "copy to ",
                    "Subproject commit ",
                )
            )
            for line in lines
        ):
            reasons.append(f"unsupported binary, mode, rename, copy, or submodule change: {file_patch.target_path}")
        if any(re.search(r"\b120000\b|\b160000\b", line) for line in lines):
            reasons.append(f"symlink or submodule mode is not allowed: {file_patch.target_path}")
        if file_patch.target_path in {"pyproject.toml", "setup.cfg"} and _touches_pytest_section(
            file_patch, repository
        ):
            reasons.append(f"pytest configuration section is protected: {file_patch.target_path}")

    banned_patterns = {
        r"\bpytest\.skip\b": "adds pytest.skip",
        r"\bpytest\.mark\.(?:skip|skipif|xfail)\b": "adds pytest skip/xfail marker",
        r"\bxfail\b": "adds xfail",
        r"--ignore(?:-glob)?\b": "adds pytest --ignore option",
        r"--maxfail\b": "adds pytest --maxfail option",
        r"(?:^|\s)(?:-k|-m|--deselect|--lf|--last-failed|--ff|--failed-first|--new-first)(?:\s|=|$)": "adds pytest test-selection option",
    }
    for line in added_lines:
        for pattern, reason in banned_patterns.items():
            if re.search(pattern, line):
                reasons.append(reason)
    return reasons


def _touches_pytest_section(file_patch: _FilePatch, repository: Path) -> bool:
    """Detect changed pytest config, including values below an existing header."""

    if file_patch.old_path is None:
        return False
    source = repository / file_patch.old_path
    if not source.is_file():
        return False
    source_lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    current_section_is_pytest = False
    for line in file_patch.lines:
        hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
        if hunk:
            current_section_is_pytest = _section_at(source_lines, int(hunk.group(1)))
            continue
        if line.startswith(" ") and re.match(r"^\s*\[[^]]+\]", line[1:]):
            current_section_is_pytest = _is_pytest_section_header(line[1:])
            continue
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        if _is_pytest_section_header(line[1:]) or current_section_is_pytest:
            return True
    return False


def _section_at(lines: list[str], line_number: int) -> bool:
    for line in reversed(lines[: max(line_number - 1, 0)]):
        header = re.match(r"^\s*\[([^]]+)\]", line)
        if header:
            name = header.group(1)
            return name == "pytest" or name.startswith("tool.pytest")
    return False


def _is_pytest_section_header(line: str) -> bool:
    header = re.match(r"^\s*\[([^]]+)\]", line)
    if header is None:
        return False
    name = header.group(1)
    return name == "pytest" or name.startswith("tool.pytest")
