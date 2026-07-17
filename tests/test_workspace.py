from pathlib import Path

from necromancer.execution.workspace import Workspace


def test_candidates_are_copied_from_the_last_accepted_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "module.py").write_text("value = 1\n", encoding="utf-8")

    workspace = Workspace.create(repository, tmp_path / "run")
    first = workspace.create_candidate("first")
    (first.path / "module.py").write_text("value = 2\n", encoding="utf-8")

    second = workspace.create_candidate("second")
    assert (second.path / "module.py").read_text(encoding="utf-8") == "value = 1\n"

    accepted = workspace.accept(first)
    assert accepted == "accepted-0001"
    third = workspace.create_candidate("third")
    assert (third.path / "module.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (repository / "module.py").read_text(encoding="utf-8") == "value = 1\n"
