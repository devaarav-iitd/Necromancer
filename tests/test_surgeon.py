from __future__ import annotations

import hashlib
import json
from pathlib import Path

from necromancer.controller.director import SurgeonContext
from necromancer.execution.scoring import BootstrapScore
from necromancer.repair.surgeon import RealSurgeon, SurgeonPatch


class _FakeClient:
    def __init__(self, patch: SurgeonPatch) -> None:
        self.patch = patch
        self.request: dict[str, object] | None = None

    def generate(self, **kwargs: object) -> SurgeonPatch:
        self.request = kwargs
        return self.patch


def test_real_surgeon_targets_innermost_non_test_traceback_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "package" / "broken.py"
    source.parent.mkdir(parents=True)
    source.write_text("from fractions import gcd\n", encoding="utf-8")
    target_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "collection": {
                    "collection_reports": [
                        {
                            "outcome": "failed",
                            "longrepr": (
                                "tests/test_broken.py:3: in <module>\n"
                                "    from package.broken import gcd\n"
                                "package/broken.py:1: in <module>\n"
                                "    from fractions import gcd\n"
                                "E   ImportError: cannot import name 'gcd' from 'fractions'"
                            ),
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    patch = SurgeonPatch(
        plan_id="fix-fractions-gcd",
        rationale="Use the current stdlib location.",
        expected_affected_tests=["tests/test_broken.py"],
        target_file="package/broken.py",
        preimage_sha256=target_hash,
        diff=(
            "diff --git a/package/broken.py b/package/broken.py\n"
            "--- a/package/broken.py\n+++ b/package/broken.py\n"
            "@@ -1 +1 @@\n-from fractions import gcd\n+from math import gcd\n"
        ),
    )
    client = _FakeClient(patch)
    surgeon = RealSurgeon(client=client, system_prompt="system")

    proposal = surgeon.propose(
        SurgeonContext(
            evaluation=1,
            best_score=BootstrapScore(False, 0, 1, 0, frozenset()),
            repository_path=repository,
            result_path=result_path,
        )
    )

    assert proposal is not None
    assert proposal.preimage_sha256 == {"package/broken.py": target_hash}
    assert "Target file: package/broken.py" in str(client.request["user_content"])
    assert "tests/test_broken.py" in str(client.request["user_content"])
