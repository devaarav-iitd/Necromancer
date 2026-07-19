from __future__ import annotations

import hashlib
from pathlib import Path

from necromancer.execution.workspace import Workspace
from necromancer.repair.patch_apply import apply_patch_to_candidate
from necromancer.repair.patch_policy import evaluate_patch


def test_accepts_and_applies_minimal_envoy_unicode_source_fix(tmp_path: Path) -> None:
    workspace, candidate = _workspace_with_envoy_source(tmp_path)
    original = (candidate.path / "envoy" / "core.py").read_bytes()
    diff = _source_fix_diff()

    result = apply_patch_to_candidate(
        candidate,
        diff,
        preimage_sha256={"envoy/core.py": _sha256(original)},
    )

    assert result.status == "applied"
    assert result.reason is None
    assert result.decision.reasons == ()
    assert "isinstance(command, (str,))" in (
        candidate.path / "envoy" / "core.py"
    ).read_text(encoding="utf-8")
    assert "unicode" in (workspace.accepted_dir / "accepted-0000" / "envoy" / "core.py").read_text(encoding="utf-8")


def test_rejects_test_file_edit_and_discards_candidate(tmp_path: Path) -> None:
    _, candidate = _workspace_with_envoy_source(tmp_path)
    original = (candidate.path / "tests" / "test_core.py").read_bytes()
    diff = """\\
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1 @@
-assert True
+assert False
"""

    result = apply_patch_to_candidate(
        candidate,
        diff,
        preimage_sha256={"tests/test_core.py": _sha256(original)},
    )

    assert result.status == "rejected"
    assert result.reason == "protected test or pytest configuration path: tests/test_core.py"
    assert not candidate.path.exists()


def test_rejects_root_level_test_module_edit(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    test_file = repository / "test_envoy.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    diff = """\\
diff --git a/test_envoy.py b/test_envoy.py
--- a/test_envoy.py
+++ b/test_envoy.py
@@ -1 +1 @@
-assert True
+assert False
"""

    decision = evaluate_patch(
        diff,
        repository,
        preimage_sha256={"test_envoy.py": _sha256(test_file.read_bytes())},
    )

    assert decision.allowed is False
    assert decision.reasons == (
        "protected test or pytest configuration path: test_envoy.py",
    )


def test_rejects_pytest_skip_injection(tmp_path: Path) -> None:
    _, candidate = _workspace_with_envoy_source(tmp_path)
    original = (candidate.path / "envoy" / "core.py").read_bytes()
    diff = """\\
diff --git a/envoy/core.py b/envoy/core.py
--- a/envoy/core.py
+++ b/envoy/core.py
@@ -1,2 +1,3 @@
+import pytest
 def expand_args(command):
+    pytest.skip("avoid legacy failure")
     if isinstance(command, (str, unicode)):
"""

    result = apply_patch_to_candidate(
        candidate,
        diff,
        preimage_sha256={"envoy/core.py": _sha256(original)},
    )

    assert result.status == "rejected"
    assert "adds pytest.skip" in result.decision.reasons
    assert not candidate.path.exists()


def test_rejects_preimage_sha_mismatch_before_git_apply(tmp_path: Path) -> None:
    _, candidate = _workspace_with_envoy_source(tmp_path)

    decision = evaluate_patch(
        _source_fix_diff(),
        candidate.path,
        preimage_sha256={"envoy/core.py": "0" * 64},
    )

    assert decision.allowed is False
    assert decision.reasons == ("preimage SHA-256 mismatch for: envoy/core.py",)
    assert candidate.path.exists()


def test_rejects_option_change_inside_existing_pytest_config_section(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    config = repository / "pyproject.toml"
    config.write_text(
        "[tool.pytest.ini_options]\naddopts = \"-q\"\n", encoding="utf-8"
    )
    diff = """\\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,2 +1,2 @@
 [tool.pytest.ini_options]
-addopts = \"-q\"
+addopts = \"-q --maxfail=1\"
"""

    decision = evaluate_patch(
        diff,
        repository,
        preimage_sha256={"pyproject.toml": _sha256(config.read_bytes())},
    )

    assert decision.allowed is False
    assert "pytest configuration section is protected: pyproject.toml" in decision.reasons


def _workspace_with_envoy_source(tmp_path: Path):
    repository = tmp_path / "source-repository"
    (repository / "envoy").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "envoy" / "core.py").write_text(
        "def expand_args(command):\n    if isinstance(command, (str, unicode)):\n        return command\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_core.py").write_text("assert True\n", encoding="utf-8")
    workspace = Workspace.create(repository, tmp_path / "workspace")
    return workspace, workspace.create_candidate("candidate")


def _source_fix_diff() -> str:
    return """\\
diff --git a/envoy/core.py b/envoy/core.py
--- a/envoy/core.py
+++ b/envoy/core.py
@@ -1,3 +1,3 @@
 def expand_args(command):
-    if isinstance(command, (str, unicode)):
+    if isinstance(command, (str,)):
         return command
"""


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()
