"""Keep revival progress grounded in runner evidence, not model judgment.

Necromancer derives every score from the ``result.json`` and ``process.json``
artifacts emitted by its deterministic runner.  No LLM input or LLM decision
is accepted by this module: a model may propose a patch, but it cannot declare
that patch successful.  In particular, the post-collection score is based on
captured per-node pytest reports, while pre-collection progress is based on
captured collection reports and installation command results.

``compute_score`` selects between the two regimes represented here.  A
successful collection produces ``TestScore``: its ``score`` tuple is
``(collection_complete, passing_count, -debt)`` and its passing node IDs are
retained for regression protection.  A failed collection produces
``BootstrapScore``: its ``score`` tracks collection completion, collected
items, collection errors, and installation errors until there is a runnable
test universe.  ``collection_frontier_advance`` handles the deliberately
bounded same-score transition between distinct collection-error frontiers.

After collection, ``accepts_test_candidate`` implements the acceptance rule:
the candidate must report complete collection, retain every node ID in the
best ``TestScore.passing_nodeids``, and have a strictly greater ``score``.

See docs/architecture.md Sessions 1-3 for full rationale and the penalty table.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Literal, TypeAlias


NodeOutcome: TypeAlias = Literal[
    "passed", "failed", "error", "skipped", "xfailed", "missing", "invalid"
]
CollectionFingerprint: TypeAlias = tuple[str, str, str]
Debt: TypeAlias = int | float


@dataclass(frozen=True)
class TestScore:
    """The post-collection score used to protect observed passing tests."""

    __test__ = False
    collection_complete: bool
    passing_nodeids: frozenset[str]
    node_outcomes: dict[str, NodeOutcome]
    debt: Debt

    @property
    def score(self) -> tuple[int, int, float]:
        return (int(self.collection_complete), len(self.passing_nodeids), -self.debt)


@dataclass(frozen=True)
class BootstrapScore:
    """The pre-collection score used while imports or collection are broken."""

    collection_complete: bool
    collected_item_count: int
    collection_error_count: int
    installation_error_count: int
    collection_error_fingerprints: frozenset[CollectionFingerprint]

    @property
    def score(self) -> tuple[int, int, int, int]:
        return (
            int(self.collection_complete),
            self.collected_item_count,
            -self.collection_error_count,
            -self.installation_error_count,
        )


Score: TypeAlias = TestScore | BootstrapScore


def compute_score(
    result_path: Path | str, *, baseline: TestScore | None = None
) -> Score:
    """Read a runner ``result.json`` and return its applicable score.

    A failed collection has no meaningful test score, so it returns a
    :class:`BootstrapScore`.  A successful collection returns a
    :class:`TestScore`; passing test identities and previously expected skips
    can then be protected against a candidate regression.
    """

    path = Path(result_path)
    result = _read_json(path)
    collection = _mapping(result.get("collection"))
    collection_complete = bool(collection.get("collection_complete"))
    fingerprints = collection_error_fingerprints(collection)
    if not collection_complete:
        return BootstrapScore(
            collection_complete=False,
            collected_item_count=len(_string_list(collection.get("collected_node_ids"))),
            collection_error_count=len(_failed_collection_reports(collection)),
            installation_error_count=_installation_error_count(result, path),
            collection_error_fingerprints=frozenset(fingerprints),
        )

    test = _mapping(result.get("test"))
    nodeids = _string_list(collection.get("collected_node_ids"))
    node_outcomes = _test_node_outcomes(test, nodeids)
    expected_nonpassing = (
        {
            nodeid
            for nodeid, outcome in baseline.node_outcomes.items()
            if outcome in {"skipped", "xfailed"}
        }
        if baseline is not None
        else {
            nodeid
            for nodeid, outcome in node_outcomes.items()
            if outcome in {"skipped", "xfailed"}
        }
    )
    debt = _debt(node_outcomes, expected_nonpassing)
    passing = frozenset(
        nodeid for nodeid, outcome in node_outcomes.items() if outcome == "passed"
    )
    return TestScore(
        collection_complete=True,
        passing_nodeids=passing,
        node_outcomes=node_outcomes,
        debt=debt,
    )


def collection_error_fingerprints(
    collection: dict[str, Any]
) -> set[CollectionFingerprint]:
    """Return stable-enough fingerprints for collection-frontier tracking."""

    fingerprints: set[CollectionFingerprint] = set()
    for report in _failed_collection_reports(collection):
        nodeid = str(report.get("nodeid", ""))
        exception_type, message = _exception_details(str(report.get("longrepr") or ""))
        fingerprints.add((exception_type, nodeid, _normalise_message(message)))
    return fingerprints


def collection_frontier_advance(
    best: BootstrapScore,
    candidate: BootstrapScore,
    *,
    seen_fingerprints: set[CollectionFingerprint] | frozenset[CollectionFingerprint],
) -> bool:
    """Allow a bounded same-score advance through a dependency/error chain."""

    if best.score != candidate.score:
        return False
    if candidate.collection_error_count > best.collection_error_count:
        return False
    disappeared = best.collection_error_fingerprints - candidate.collection_error_fingerprints
    newly_exposed = (
        candidate.collection_error_fingerprints - best.collection_error_fingerprints
    )
    return bool(disappeared) and all(
        fingerprint not in seen_fingerprints for fingerprint in newly_exposed
    )


def accepts_test_candidate(best: TestScore, candidate: TestScore) -> bool:
    """Implement the protected-pass acceptance rule after collection succeeds."""

    return (
        candidate.collection_complete
        and best.passing_nodeids.issubset(candidate.passing_nodeids)
        and candidate.score > best.score
    )


def _test_node_outcomes(test: dict[str, Any], nodeids: list[str]) -> dict[str, NodeOutcome]:
    if test.get("capture_status") != "complete":
        return {nodeid: "invalid" for nodeid in nodeids}

    reports_by_node: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_report in _list_of_mappings(test.get("test_reports")):
        nodeid = raw_report.get("nodeid")
        if isinstance(nodeid, str):
            reports_by_node[nodeid].append(raw_report)
    return {
        nodeid: _classify_node_reports(reports_by_node[nodeid]) for nodeid in nodeids
    }


def _classify_node_reports(reports: list[dict[str, Any]]) -> NodeOutcome:
    if not reports:
        return "missing"
    if any(report.get("outcome") == "error" for report in reports):
        return "error"
    if any(
        report.get("outcome") == "failed"
        and report.get("when") in {"setup", "teardown"}
        for report in reports
    ):
        return "error"
    if any(
        report.get("outcome") == "failed" and report.get("when") == "call"
        for report in reports
    ):
        return "failed"
    if any(report.get("outcome") == "skipped" for report in reports):
        return (
            "xfailed"
            if any(report.get("wasxfail") for report in reports)
            else "skipped"
        )
    if any(
        report.get("outcome") == "passed" and report.get("when") == "call"
        for report in reports
    ):
        return "passed"
    return "missing"


def _debt(
    node_outcomes: dict[str, NodeOutcome], expected_nonpassing: set[str]
) -> Debt:
    total = 0
    for nodeid, outcome in node_outcomes.items():
        if outcome == "invalid":
            return math.inf
        if outcome == "passed":
            continue
        if outcome in {"skipped", "xfailed"}:
            total += 0 if nodeid in expected_nonpassing else 4
        elif outcome == "failed":
            total += 2
        elif outcome == "error":
            total += 3
        elif outcome == "missing":
            total += 5
    return total


def _failed_collection_reports(collection: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        report
        for report in _list_of_mappings(collection.get("collection_reports"))
        if report.get("outcome") == "failed"
    ]


def _installation_error_count(result: dict[str, Any], result_path: Path) -> int:
    process_path_value = result.get("process_path")
    if not isinstance(process_path_value, str):
        return 0
    process_path = Path(process_path_value)
    if not process_path.is_absolute():
        process_path = result_path.parent / process_path
    if not process_path.is_file():
        return 0
    process = _read_json(process_path)
    installation_steps = {"create_venv", "bootstrap_pytest", "install_project"}
    errors = 0
    for command in _list_of_mappings(process.get("commands")):
        if command.get("name") not in installation_steps:
            continue
        if command.get("status") != "completed" or command.get("return_code") != 0:
            errors += 1
    return errors


def _exception_details(longrepr: str) -> tuple[str, str]:
    matches = re.findall(
        r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit))(?::\s*(.*))?$",
        longrepr,
        flags=re.MULTILINE,
    )
    if matches:
        exception_type, message = matches[-1]
        return exception_type, message
    first_line = next((line.strip() for line in longrepr.splitlines() if line.strip()), "")
    return "UnknownCollectionError", first_line


def _normalise_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return document


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_mappings(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
