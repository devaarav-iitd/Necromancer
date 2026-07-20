"""Immutable source snapshots and disposable candidate working directories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4


_IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".necromancer",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "venv",
}


@dataclass(frozen=True)
class CandidateSnapshot:
    """A writable, disposable copy of the workspace's accepted snapshot."""

    identifier: str
    path: Path
    artifact_dir: Path
    parent_accepted_id: str


class Workspace:
    """Owns a source snapshot, immutable accepted states, and candidates.

    The original repository is read only.  A candidate is always copied from the
    current accepted snapshot, so a rejected attempt can be discarded without a
    rollback operation or a mutation of the user's checkout.
    """

    STATE_FILE = "workspace.json"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.source_dir = self.root / "source"
        self.accepted_dir = self.root / "accepted"
        self.candidates_dir = self.root / "candidates"
        self.artifacts_dir = self.root / "artifacts"
        self.state_path = self.root / self.STATE_FILE

    @classmethod
    def create(cls, repository: Path, root: Path) -> "Workspace":
        """Copy ``repository`` into a newly-created immutable workspace.

        ``root`` must not already contain a workspace.  VCS metadata, virtual
        environments, Python caches, and prior Necromancer artifacts are omitted
        from snapshots because they are mutable execution state, not source.
        """

        repository = repository.expanduser().resolve()
        root = root.expanduser().resolve()
        if not repository.is_dir():
            raise ValueError(f"Repository path is not a directory: {repository}")
        if root == repository or root.is_relative_to(repository):
            raise ValueError("Workspace root must not be inside the source repository")
        if root.exists() and any(root.iterdir()):
            raise ValueError(f"Workspace root must be empty: {root}")

        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(root)
        workspace.accepted_dir.mkdir()
        workspace.candidates_dir.mkdir()
        workspace.artifacts_dir.mkdir()

        shutil.copytree(
            repository,
            workspace.source_dir,
            symlinks=True,
            ignore=_snapshot_ignore,
        )
        _make_read_only(workspace.source_dir)

        initial_id = "accepted-0000"
        initial_dir = workspace.accepted_dir / initial_id
        shutil.copytree(workspace.source_dir, initial_dir, symlinks=True)
        _make_read_only(initial_dir)
        workspace._write_state(
            {
                "schema_version": 1,
                "created_at": _utc_now(),
                "original_repository": str(repository),
                "accepted_id": initial_id,
                "next_accepted_number": 1,
            }
        )
        return workspace

    @classmethod
    def open(cls, root: Path) -> "Workspace":
        workspace = cls(root)
        if not workspace.state_path.is_file():
            raise ValueError(f"Not a Necromancer workspace: {workspace.root}")
        return workspace

    def create_candidate(self, identifier: str | None = None) -> CandidateSnapshot:
        """Create a new writable candidate from the most recently accepted state."""

        state = self._read_state()
        candidate_id = identifier or f"candidate-{uuid4().hex[:12]}"
        _validate_snapshot_identifier(candidate_id)
        destination = self.candidates_dir / candidate_id
        if destination.exists():
            raise ValueError(f"Candidate already exists: {candidate_id}")

        accepted_id = str(state["accepted_id"])
        accepted_path = self.accepted_dir / accepted_id
        if not accepted_path.is_dir():
            raise RuntimeError(f"Accepted snapshot is missing: {accepted_path}")

        shutil.copytree(accepted_path, destination, symlinks=True)
        _make_writable(destination)
        artifact_dir = self.artifacts_dir / candidate_id
        artifact_dir.mkdir(parents=True)
        return CandidateSnapshot(candidate_id, destination, artifact_dir, accepted_id)

    def clone_candidate(
        self, source: CandidateSnapshot, identifier: str
    ) -> CandidateSnapshot:
        """Create a fresh candidate from a disposable candidate's exact state.

        Director uses this only to preserve a bounded staged bundle while an
        individual patch is retried.  The accepted snapshot remains untouched.
        """

        source_path = source.path.resolve()
        if source_path.parent != self.candidates_dir or not source_path.is_dir():
            raise ValueError("Only a candidate from this workspace may be cloned")
        _validate_snapshot_identifier(identifier)
        destination = self.candidates_dir / identifier
        if destination.exists():
            raise ValueError(f"Candidate already exists: {destination}")
        shutil.copytree(source_path, destination, symlinks=True)
        _make_writable(destination)
        artifact_dir = self.artifacts_dir / identifier
        artifact_dir.mkdir(parents=True)
        return CandidateSnapshot(
            identifier, destination, artifact_dir, source.parent_accepted_id
        )

    def accept(self, candidate: CandidateSnapshot) -> str:
        """Promote a candidate by copying it into a new immutable accepted state."""

        candidate_path = candidate.path.resolve()
        if candidate_path.parent != self.candidates_dir or not candidate_path.is_dir():
            raise ValueError("Only a candidate from this workspace may be accepted")

        state = self._read_state()
        accepted_id = f"accepted-{int(state['next_accepted_number']):04d}"
        destination = self.accepted_dir / accepted_id
        shutil.copytree(candidate_path, destination, symlinks=True)
        _make_read_only(destination)
        state["accepted_id"] = accepted_id
        state["next_accepted_number"] = int(state["next_accepted_number"]) + 1
        state["accepted_at"] = _utc_now()
        self._write_state(state)
        return accepted_id

    def discard(self, candidate: CandidateSnapshot) -> None:
        """Remove a disposable candidate and its execution artifacts."""

        candidate_path = candidate.path.resolve()
        if candidate_path.parent != self.candidates_dir:
            raise ValueError("Only a candidate from this workspace may be discarded")
        if candidate_path.exists():
            _make_writable(candidate_path)
            shutil.rmtree(candidate_path)
        if candidate.artifact_dir.exists():
            shutil.rmtree(candidate.artifact_dir)

    def _read_state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, object]) -> None:
        temporary_path = self.state_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary_path, self.state_path)


def _snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in _IGNORED_DIRECTORY_NAMES or name.endswith(".egg-info"):
            ignored.add(name)
    return ignored


def _make_read_only(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod((mode & ~0o222) | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            path.chmod(mode & ~0o222)
    directory.chmod((directory.stat().st_mode & ~0o222) | 0o555)


def _make_writable(directory: Path) -> None:
    for path in [directory, *directory.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(mode | stat.S_IWUSR | stat.S_IXUSR)
        else:
            path.chmod(mode | stat.S_IWUSR)


def _validate_snapshot_identifier(identifier: str) -> None:
    if not identifier or identifier in {".", ".."}:
        raise ValueError("Candidate identifier must be non-empty")
    if Path(identifier).name != identifier:
        raise ValueError("Candidate identifier must be a single path component")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
