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
    assert result.status == "accepted"
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
