from __future__ import annotations

import json
from pathlib import Path

from necromancer.execution.scoring import (
    BootstrapScore,
    TestScore,
    accepts_test_candidate,
    collection_frontier_advance,
    compute_score,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_envoy_real_result_uses_test_score_with_nine_failed_tests() -> None:
    score = compute_score(_real_result("envoy-*"))

    assert isinstance(score, TestScore)
    assert score.collection_complete is True
    assert score.passing_nodeids == frozenset()
    assert set(score.node_outcomes.values()) == {"failed"}
    assert len(score.node_outcomes) == 9
    assert score.debt == 18
    assert score.score == (1, 0, -18)


def test_vincent_real_result_uses_bootstrap_not_test_score() -> None:
    score = compute_score(_real_result("vincent-*"))

    assert isinstance(score, BootstrapScore)
    assert score.collection_complete is False
    assert score.collected_item_count == 0
    # Vincent has three failed test-module collection reports: nose once and
    # pkg_resources twice. They represent two missing dependencies, but the
    # bootstrap score deliberately counts reports rather than root-cause types.
    assert score.collection_error_count == 3
    assert score.installation_error_count == 0
    assert score.score == (0, 0, -3, 0)
    assert {fingerprint[0] for fingerprint in score.collection_error_fingerprints} == {
        "ModuleNotFoundError"
    }
    assert any("No module named 'nose'" in fingerprint[2] for fingerprint in score.collection_error_fingerprints)


def test_same_score_collection_frontier_can_advance_once_without_revisiting() -> None:
    old_error = ("ImportError", "tests/test_old.py", "No module named 'old_api'")
    new_error = ("ImportError", "tests/test_next.py", "No module named 'next_api'")
    best = BootstrapScore(False, 0, 1, 0, frozenset({old_error}))
    candidate = BootstrapScore(False, 0, 1, 0, frozenset({new_error}))

    assert collection_frontier_advance(
        best, candidate, seen_fingerprints={old_error}
    )
    assert not collection_frontier_advance(
        best, candidate, seen_fingerprints={old_error, new_error}
    )


def test_protected_pass_acceptance_requires_strict_progress() -> None:
    best = TestScore(True, frozenset({"tests/test_api.py::test_old"}), {}, debt=2)
    improved = TestScore(
        True,
        frozenset({"tests/test_api.py::test_old", "tests/test_api.py::test_new"}),
        {},
        debt=0,
    )
    regressed = TestScore(True, frozenset({"tests/test_api.py::test_new"}), {}, debt=0)

    assert accepts_test_candidate(best, improved)
    assert not accepts_test_candidate(best, regressed)


def test_collection_succeeded_but_nothing_passed_currently_collapses_states(
    tmp_path: Path,
) -> None:
    """Characterize the current score before adding any new score dimension.

    Case A and B have one collected test each. Case C intentionally represents
    a controller input that says collection succeeded but supplies no node IDs;
    the real runner normally reports pytest exit code 5 instead, so this makes
    the scoring layer's behavior explicit if such an artifact reaches it.
    """

    xfail = compute_score(
        _write_result(
            tmp_path / "xfail.json",
            nodeids=["tests/test_legacy.py::test_old_api"],
            reports=[
                _report(
                    "tests/test_legacy.py::test_old_api",
                    outcome="skipped",
                    wasxfail="known Python 2 compatibility breakage",
                )
            ],
        )
    )
    dependency_skip = compute_score(
        _write_result(
            tmp_path / "dependency_skip.json",
            nodeids=["tests/test_optional.py::test_export"],
            reports=[
                _report("tests/test_optional.py::test_export", outcome="skipped")
            ],
        )
    )
    zero_items = compute_score(_write_result(tmp_path / "zero_items.json", nodeids=[]))
    all_passing = compute_score(
        _write_result(
            tmp_path / "passing.json",
            nodeids=["tests/test_working.py::test_ok"],
            reports=[_report("tests/test_working.py::test_ok", outcome="passed")],
        )
    )
    envoy = compute_score(_real_result("envoy-*"))

    assert all(isinstance(score, TestScore) for score in (xfail, dependency_skip, zero_items))
    # A and B are intentionally compared before deciding whether they should
    # become separate score states. The current tuple deliberately collapses
    # acknowledged xfails and baseline-known skips to zero debt.
    assert xfail.score == (1, 0, 0)
    assert dependency_skip.score == (1, 0, 0)
    # C exposes a separate gap: with zero collected node IDs, the current score
    # has no debt to charge and therefore also collapses to the same tuple.
    assert zero_items.score == (1, 0, 0)
    assert xfail.score == dependency_skip.score == zero_items.score
    assert xfail.score != envoy.score
    assert xfail.score != all_passing.score
    assert all_passing.score == (1, 1, 0)


def _write_result(
    path: Path,
    *,
    nodeids: list[str],
    reports: list[dict[str, object]] | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collection": {
                    "collection_complete": True,
                    "collected_node_ids": nodeids,
                    "collection_reports": [],
                },
                "test": {
                    "capture_status": "complete",
                    "test_reports": reports or [],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _report(
    nodeid: str, *, outcome: str, wasxfail: str | None = None
) -> dict[str, object]:
    return {
        "nodeid": nodeid,
        "outcome": outcome,
        "when": "call",
        "wasxfail": wasxfail,
    }


def _real_result(run_glob: str) -> Path:
    results = list(
        (REPOSITORY_ROOT / ".necromancer-runs").glob(
            f"{run_glob}/artifacts/*/result.json"
        )
    )
    assert results, f"No local result artifact found for {run_glob!r}"
    return max(results, key=lambda path: path.stat().st_mtime)
