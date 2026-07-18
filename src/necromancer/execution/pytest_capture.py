"""A tiny pytest plugin that emits deterministic execution evidence.

The plugin is loaded explicitly by :mod:`necromancer.execution.pytest_runner`.
It deliberately has no dependency beyond pytest and writes its JSON outside the
candidate repository, as selected by ``NECROMANCER_RESULT_PATH``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any

import pytest


RESULT_PATH_ENV = "NECROMANCER_RESULT_PATH"
_ACTIVE_STATE: dict[str, Any] | None = None
_PYTEST_ROOT: Path | None = None
_WORKING_DIRECTORY: Path | None = None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    global _ACTIVE_STATE, _PYTEST_ROOT, _WORKING_DIRECTORY
    state = {
        "schema_version": 1,
        "capture_status": "capturing",
        "started_at": _utc_now(),
        "collected_node_ids": [],
        "collection_reports": [],
        "test_reports": [],
        "session_exit_status": None,
        "session_exit_name": None,
        "tests_collected": 0,
    }
    _ACTIVE_STATE = state
    _PYTEST_ROOT = Path(str(config.rootpath)).resolve()
    _WORKING_DIRECTORY = Path.cwd().resolve()
    setattr(config, "_necromancer_capture_state", state)


@pytest.hookimpl(tryfirst=True)
def pytest_collectreport(report: pytest.CollectReport) -> None:
    state = _active_state()
    if state is not None:
        state["collection_reports"].append(_report_to_dict(report))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    state = _state(config)
    state["collected_node_ids"] = [_normalise_nodeid(item.nodeid) for item in items]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    state = _active_state()
    if state is not None:
        state["test_reports"].append(_report_to_dict(report))


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    state = _state(session.config)
    status = int(exitstatus)
    state["session_exit_status"] = status
    state["session_exit_name"] = _exit_name(status)
    state["tests_collected"] = int(session.testscollected)
    state["finished_at"] = _utc_now()
    state["capture_status"] = "complete"
    _write_state(session.config, state)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """Leave an artifact for early failures that bypass sessionfinish."""

    state = _state(config)
    if state.get("capture_status") != "complete":
        state["finished_at"] = _utc_now()
        state["capture_status"] = "incomplete"
        _write_state(config, state)


def _state(config: pytest.Config) -> dict[str, Any]:
    return getattr(config, "_necromancer_capture_state")


def _active_state() -> dict[str, Any] | None:
    return _ACTIVE_STATE


def _report_to_dict(report: pytest.CollectReport | pytest.TestReport) -> dict[str, Any]:
    location = getattr(report, "location", None)
    return {
        "nodeid": _normalise_nodeid(report.nodeid),
        "outcome": report.outcome,
        "when": getattr(report, "when", "collect"),
        "duration_seconds": getattr(report, "duration", 0.0),
        "location": list(location) if location is not None else None,
        "longrepr": _longrepr(report),
        "wasxfail": getattr(report, "wasxfail", None),
    }


def _longrepr(report: pytest.CollectReport | pytest.TestReport) -> str | None:
    longrepr = getattr(report, "longrepr", None)
    return None if longrepr is None else str(longrepr)


def _normalise_nodeid(nodeid: str) -> str:
    """Make a node ID independent of this candidate snapshot's directory."""

    if not nodeid or _PYTEST_ROOT is None or _WORKING_DIRECTORY is None:
        return nodeid
    path_part, separator, suffix = nodeid.partition("::")
    if not path_part:
        return nodeid
    candidate_path = Path(path_part)
    resolved = (
        candidate_path.resolve()
        if candidate_path.is_absolute()
        else (_PYTEST_ROOT / candidate_path).resolve()
    )
    try:
        stable_path = resolved.relative_to(_WORKING_DIRECTORY).as_posix()
    except ValueError:
        return nodeid
    return stable_path if not separator else f"{stable_path}{separator}{suffix}"


def _write_state(config: pytest.Config, state: dict[str, Any]) -> None:
    output = os.environ.get(RESULT_PATH_ENV)
    if not output:
        return
    destination = Path(output)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, destination)
    except OSError:
        # Capturing evidence must never change the repository's pytest outcome.
        return


def _exit_name(status: int) -> str:
    try:
        return pytest.ExitCode(status).name
    except ValueError:
        return "UNKNOWN"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
