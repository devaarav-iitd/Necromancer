"""Install a candidate, establish collection state, then run pytest."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

from necromancer.execution.pytest_capture import RESULT_PATH_ENV


@dataclass(frozen=True)
class RunnerConfig:
    install_timeout_seconds: float = 60.0
    collection_timeout_seconds: float = 30.0
    test_timeout_seconds: float = 60.0
    # This allows the MVP to run offline using a developer-provisioned pytest.
    # The wheelhouse milestone will replace it with a pinned harness environment.
    inherit_system_site_packages: bool = True
    bootstrap_pytest_requirement: str | None = None
    no_build_isolation: bool = True


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: list[str]
    status: str
    return_code: int | None
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    launch_error: str | None = None


@dataclass(frozen=True)
class RunResult:
    artifact_dir: Path
    result_path: Path
    process_path: Path
    collection_complete: bool
    full_run_started: bool


class PytestRunner:
    """Run a candidate in a new venv and emit durable evidence artifacts."""

    def __init__(self, config: RunnerConfig | None = None) -> None:
        self.config = config or RunnerConfig()
        self._capture_import_root = Path(__file__).resolve().parents[2]

    def run(self, repository: Path, artifact_dir: Path) -> RunResult:
        repository = repository.resolve()
        artifact_dir = artifact_dir.resolve()
        if not repository.is_dir():
            raise ValueError(f"Repository path is not a directory: {repository}")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        process_path = artifact_dir / "process.json"
        summary_path = artifact_dir / "result.json"
        commands: list[CommandResult] = []

        venv_dir = artifact_dir / "venv"
        venv_python = _venv_python(venv_dir)
        create_venv_command = [sys.executable, "-m", "venv"]
        if self.config.inherit_system_site_packages:
            create_venv_command.append("--system-site-packages")
        create_venv_command.append(str(venv_dir))
        commands.append(
            _run_command(
                name="create_venv",
                command=create_venv_command,
                cwd=artifact_dir,
                environment=_base_environment(),
                timeout_seconds=self.config.install_timeout_seconds,
                artifact_dir=artifact_dir,
            )
        )
        if not _command_succeeded(commands[-1]):
            return self._finish(
                summary_path, process_path, commands, collection=None, test=None
            )

        bootstrap_command = (
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                self.config.bootstrap_pytest_requirement,
            ]
            if self.config.bootstrap_pytest_requirement is not None
            else [str(venv_python), "-c", "import pytest; print(pytest.__version__)"]
        )
        commands.append(
            _run_command(
                name="bootstrap_pytest",
                command=bootstrap_command,
                cwd=repository,
                environment=_base_environment(),
                timeout_seconds=self.config.install_timeout_seconds,
                artifact_dir=artifact_dir,
            )
        )
        if not _command_succeeded(commands[-1]):
            return self._finish(
                summary_path, process_path, commands, collection=None, test=None
            )

        install_project_command = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]
        if self.config.no_build_isolation:
            install_project_command.append("--no-build-isolation")
        install_project_command.extend(["--editable", str(repository)])
        commands.append(
            _run_command(
                name="install_project",
                command=install_project_command,
                cwd=repository,
                environment=_base_environment(),
                timeout_seconds=self.config.install_timeout_seconds,
                artifact_dir=artifact_dir,
            )
        )
        if not _command_succeeded(commands[-1]):
            return self._finish(
                summary_path, process_path, commands, collection=None, test=None
            )

        collection_path = artifact_dir / "collection" / "result.json"
        collection_command = [
            str(venv_python),
            "-m",
            "pytest",
            "-p",
            "necromancer.execution.pytest_capture",
            "-q",
            f"--rootdir={repository}",
            "--collect-only",
            f"--junitxml={artifact_dir / 'collection' / 'junit.xml'}",
        ]
        commands.append(
            _run_command(
                name="collect",
                command=collection_command,
                cwd=repository,
                environment=_pytest_environment(collection_path),
                timeout_seconds=self.config.collection_timeout_seconds,
                artifact_dir=artifact_dir,
            )
        )
        collection = _load_phase_result(collection_path, commands[-1], "collection")
        if not _collection_complete(collection, commands[-1]):
            return self._finish(
                summary_path, process_path, commands, collection=collection, test=None
            )

        test_path = artifact_dir / "test" / "result.json"
        test_command = [
            str(venv_python),
            "-m",
            "pytest",
            "-p",
            "necromancer.execution.pytest_capture",
            "-q",
            f"--rootdir={repository}",
            f"--junitxml={artifact_dir / 'test' / 'junit.xml'}",
        ]
        commands.append(
            _run_command(
                name="test",
                command=test_command,
                cwd=repository,
                environment=_pytest_environment(test_path),
                timeout_seconds=self.config.test_timeout_seconds,
                artifact_dir=artifact_dir,
            )
        )
        test = _load_phase_result(test_path, commands[-1], "test")
        return self._finish(
            summary_path, process_path, commands, collection=collection, test=test
        )

    def _finish(
        self,
        summary_path: Path,
        process_path: Path,
        commands: list[CommandResult],
        *,
        collection: dict[str, Any] | None,
        test: dict[str, Any] | None,
    ) -> RunResult:
        process_document = {
            "schema_version": 1,
            "commands": [asdict(command) for command in commands],
        }
        _write_json(process_path, process_document)
        collection_complete = bool(
            collection
            and collection.get("collection_complete")
            and collection.get("capture_status") == "complete"
        )
        summary = {
            "schema_version": 1,
            "collection": collection
            if collection is not None
            else _not_started_phase("collection", commands),
            "test": test
            if test is not None
            else _not_started_phase(
                "test", commands, "collection did not complete successfully"
            ),
            "process_path": str(process_path),
        }
        _write_json(summary_path, summary)
        return RunResult(
            artifact_dir=summary_path.parent,
            result_path=summary_path,
            process_path=process_path,
            collection_complete=collection_complete,
            full_run_started=test is not None,
        )


def _run_command(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    artifact_dir: Path,
) -> CommandResult:
    started_at = _utc_now()
    started = time.monotonic()
    stdout_path = artifact_dir / f"{name}.stdout.log"
    stderr_path = artifact_dir / f"{name}.stderr.log"
    status = "completed"
    return_code: int | None = None
    launch_error: str | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        status = "timed_out"
        stdout = _coerce_output(error.stdout)
        stderr = _coerce_output(error.stderr)
    except OSError as error:
        status = "launch_error"
        launch_error = str(error)
        stderr = str(error)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return CommandResult(
        name=name,
        command=list(command),
        status=status,
        return_code=return_code,
        started_at=started_at,
        finished_at=_utc_now(),
        duration_seconds=round(time.monotonic() - started, 6),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        launch_error=launch_error,
    )


def _load_phase_result(
    path: Path, command: CommandResult, phase: str
) -> dict[str, Any]:
    if path.is_file():
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return _unavailable_phase(phase, command, "capture result was invalid JSON")
        result["collection_complete"] = (
            _is_collection_complete(result, command)
            if phase == "collection"
            else _capture_collection_completed(result)
        )
        return result
    return _unavailable_phase(phase, command, "pytest capture plugin wrote no result")


def _is_collection_complete(result: dict[str, Any], command: CommandResult) -> bool:
    if command.status != "completed" or command.return_code != 0:
        return False
    if result.get("capture_status") != "complete":
        return False
    reports = result.get("collection_reports", [])
    return not any(report.get("outcome") == "failed" for report in reports)


def _collection_complete(result: dict[str, Any], command: CommandResult) -> bool:
    return bool(result.get("collection_complete")) and _command_succeeded(command)


def _capture_collection_completed(result: dict[str, Any]) -> bool:
    if result.get("capture_status") != "complete":
        return False
    reports = result.get("collection_reports", [])
    return not any(report.get("outcome") == "failed" for report in reports)


def _unavailable_phase(
    phase: str, command: CommandResult, reason: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": phase,
        "capture_status": "unavailable",
        "collection_complete": False,
        "reason": reason,
        "command_status": command.status,
        "session_exit_status": command.return_code,
        "collected_node_ids": [],
        "collection_reports": [],
        "test_reports": [],
    }


def _not_started_phase(
    phase: str, commands: list[CommandResult], reason: str | None = None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": phase,
        "capture_status": "not_started",
        "collection_complete": False,
        "reason": reason or _first_unsuccessful_command_reason(commands),
        "collected_node_ids": [],
        "collection_reports": [],
        "test_reports": [],
        "session_exit_status": None,
    }


def _first_unsuccessful_command_reason(commands: list[CommandResult]) -> str:
    for command in commands:
        if not _command_succeeded(command):
            return f"{command.name} did not complete successfully"
    return "phase was not started"


def _command_succeeded(command: CommandResult) -> bool:
    return command.status == "completed" and command.return_code == 0


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _base_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _pytest_environment(result_path: Path) -> dict[str, str]:
    environment = _base_environment()
    existing_python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in [str(Path(__file__).resolve().parents[2]), existing_python_path] if part
    )
    environment[RESULT_PATH_ENV] = str(result_path)
    return environment


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
