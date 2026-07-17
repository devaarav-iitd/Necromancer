import json
from pathlib import Path

from necromancer.execution.pytest_runner import PytestRunner, RunnerConfig


def test_collection_error_is_captured_as_a_collection_failure(tmp_path: Path) -> None:
    repository = tmp_path / "broken-repository"
    repository.mkdir()
    (repository / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='broken-repository')\n",
        encoding="utf-8",
    )
    (repository / "test_import_failure.py").write_text(
        "raise NameError('unicode is not defined during collection')\n",
        encoding="utf-8",
    )

    result = PytestRunner(
        RunnerConfig(
            install_timeout_seconds=60,
            collection_timeout_seconds=30,
            test_timeout_seconds=30,
        )
    ).run(repository, tmp_path / "artifacts")

    document = json.loads(result.result_path.read_text(encoding="utf-8"))
    collection = document["collection"]
    assert result.collection_complete is False
    assert result.full_run_started is False
    assert collection["capture_status"] == "complete"
    assert collection["session_exit_status"] != 0
    assert collection["test_reports"] == []
    assert any(report["outcome"] == "failed" for report in collection["collection_reports"])
    assert "NameError" in "\n".join(
        str(report["longrepr"]) for report in collection["collection_reports"]
    )
    assert document["test"]["capture_status"] == "not_started"
    assert (tmp_path / "artifacts" / "collection" / "junit.xml").is_file()


def test_runtime_failure_is_not_misclassified_as_a_collection_failure(tmp_path: Path) -> None:
    repository = tmp_path / "runtime-failure"
    repository.mkdir()
    (repository / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='runtime-failure')\n",
        encoding="utf-8",
    )
    (repository / "test_runtime_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    result = PytestRunner().run(repository, tmp_path / "artifacts")
    document = json.loads(result.result_path.read_text(encoding="utf-8"))

    assert result.collection_complete is True
    assert result.full_run_started is True
    assert document["collection"]["collection_complete"] is True
    assert document["test"]["collection_complete"] is True
    assert document["test"]["session_exit_status"] == 1
    assert any(
        report["when"] == "call" and report["outcome"] == "failed"
        for report in document["test"]["test_reports"]
    )
