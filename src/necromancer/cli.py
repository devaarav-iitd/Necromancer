"""Small CLI surface for the deterministic MVP."""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

from necromancer.execution.pytest_runner import PytestRunner, RunnerConfig
from necromancer.execution.workspace import Workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="necromancer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="Create an isolated candidate and run its pytest suite."
    )
    run_parser.add_argument("repository", type=Path)
    run_parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(".necromancer-runs"),
        help="Directory in which to create this isolated run.",
    )
    run_parser.add_argument("--install-timeout", type=float, default=60.0)
    run_parser.add_argument("--collection-timeout", type=float, default=30.0)
    run_parser.add_argument("--test-timeout", type=float, default=60.0)

    args = parser.parse_args(argv)
    if args.command != "run":  # pragma: no cover - argparse guarantees this.
        return 2

    repository_name = args.repository.expanduser().resolve().name or "repository"
    run_root = args.workspace_root / f"{repository_name}-{uuid4().hex[:12]}"
    workspace = Workspace.create(args.repository, run_root)
    candidate = workspace.create_candidate()
    runner = PytestRunner(
        RunnerConfig(
            install_timeout_seconds=args.install_timeout,
            collection_timeout_seconds=args.collection_timeout,
            test_timeout_seconds=args.test_timeout,
        )
    )
    result = runner.run(candidate.path, candidate.artifact_dir)
    print(result.result_path)
    return 0
