from __future__ import annotations

import hashlib
import json
from pathlib import Path

from necromancer.controller.director import (
    Director,
    DirectorConfig,
    FixtureSurgeon,
    PatchProposal,
)
from necromancer.execution.scoring import TestScore
from necromancer.execution.workspace import Workspace


def test_director_promotes_a_stubbed_source_fix_after_a_score_improvement(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='director-fixture', py_modules=['module'])\n",
        encoding="utf-8",
    )
    source = repository / "module.py"
    source.write_text("def value():\n    return 'old'\n", encoding="utf-8")
    (repository / "test_module.py").write_text(
        "from module import value\n\ndef test_value():\n    assert value() == 'new'\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "patch.json"
    fixture.write_text(
        json.dumps(
            {
                "diff": "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 'old'\n+    return 'new'\n",
                "preimage_sha256": {
                    "module.py": hashlib.sha256(source.read_bytes()).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )

    workspace = Workspace.create(repository, tmp_path / "workspace")
    result = Director(config=DirectorConfig(max_evaluations=1)).revive(
        workspace, FixtureSurgeon.from_fixture(fixture)
    )

    assert isinstance(result.baseline_score, TestScore)
    assert isinstance(result.final_score, TestScore)
    assert result.baseline_score.score == (1, 0, -2)
    assert result.final_score.score == (1, 1, 0)
    assert result.accepted_snapshot_id == "accepted-0001"
    assert result.status == "full_revival"
    assert result.review_records == ()
    assert [event.status for event in result.events] == ["baseline", "accepted"]


def test_director_can_stage_a_bounded_two_patch_bundle(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='director-staged', py_modules=['module'])\n",
        encoding="utf-8",
    )
    source = repository / "module.py"
    initial = "NOTE = 'old'\n\ndef value():\n    return 'old'\n"
    after_first = "NOTE = 'staged'\n\ndef value():\n    return 'old'\n"
    source.write_text(initial, encoding="utf-8")
    (repository / "test_module.py").write_text(
        "from module import value\n\ndef test_value():\n    assert value() == 'new'\n",
        encoding="utf-8",
    )
    first = PatchProposal(
        diff=(
            "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n"
            "@@ -1,3 +1,3 @@\n-NOTE = 'old'\n+NOTE = 'staged'\n \n def value():\n"
        ),
        preimage_sha256={"module.py": hashlib.sha256(initial.encode()).hexdigest()},
    )
    second = PatchProposal(
        diff=(
            "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n"
            "@@ -2,3 +2,3 @@ NOTE = 'staged'\n \n def value():\n-    return 'old'\n+    return 'new'\n"
        ),
        preimage_sha256={"module.py": hashlib.sha256(after_first.encode()).hexdigest()},
    )

    workspace = Workspace.create(repository, tmp_path / "workspace")
    result = Director(
        config=DirectorConfig(max_evaluations=2, max_staged_patches=2)
    ).revive(workspace, FixtureSurgeon((first, second)))

    assert result.baseline_score.score == (1, 0, -2)
    assert result.final_score.score == (1, 1, 0)
    assert result.accepted_snapshot_id == "accepted-0001"
    assert [event.status for event in result.events] == [
        "baseline",
        "staged",
        "accepted",
    ]


def test_director_reports_a_protected_python2_test_failure_for_human_review(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='director-partial', py_modules=['module'])\n",
        encoding="utf-8",
    )
    source = repository / "module.py"
    source.write_text("def value():\n    return 'old'\n", encoding="utf-8")
    (repository / "test_module.py").write_text(
        "import subprocess\n\n"
        "from module import value\n\n"
        "def test_value():\n"
        "    assert value() == 'new'\n\n"
        "def test_legacy_command():\n"
        "    command = \"python -c 'print \\\"legacy\\\"'\"\n"
        "    completed = subprocess.run(command, shell=True, capture_output=True, text=True)\n"
        "    assert completed.stdout.rstrip() == 'legacy'\n",
        encoding="utf-8",
    )
    proposal = PatchProposal(
        diff=(
            "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n"
            "@@ -1,2 +1,2 @@\n def value():\n-    return 'old'\n+    return 'new'\n"
        ),
        preimage_sha256={"module.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    )

    workspace = Workspace.create(repository, tmp_path / "workspace")
    result = Director(config=DirectorConfig(max_evaluations=1)).revive(
        workspace, FixtureSurgeon((proposal,))
    )

    assert result.status == "partial_revival"
    assert result.final_score.score == (1, 1, -2)
    assert result.accepted_snapshot_id == "accepted-0001"
    assert result.review_records[0].nodeid == "test_module.py::test_legacy_command"
    assert "protected test file contains Python-2 syntax" in result.review_records[0].reason
    assert "test_module.py:9 contains Python-2 print syntax" in result.review_records[0].evidence
