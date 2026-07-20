"""Small CLI surface for the deterministic MVP."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from necromancer.controller.director import Director, DirectorConfig, FixtureSurgeon
from necromancer.execution.pytest_runner import PytestRunner, RunnerConfig
from necromancer.execution.workspace import Workspace
from necromancer.llm.client import LLMClientError
from necromancer.repair.surgeon import RealSurgeon


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
    run_parser.add_argument(
        "--bootstrap-pytest-requirement",
        help="Install this pytest requirement into each disposable runner venv.",
    )
    run_parser.add_argument(
        "--build-isolation",
        action="store_true",
        help="Allow pip build isolation when installing the target repository.",
    )

    revive_parser = subparsers.add_parser(
        "revive", help="Run the Director with a real GPT Surgeon or fixtures."
    )
    revive_parser.add_argument("repository", type=Path)
    revive_parser.add_argument(
        "--workspace-root", type=Path, default=Path(".necromancer-runs")
    )
    revive_parser.add_argument("--install-timeout", type=float, default=60.0)
    revive_parser.add_argument("--collection-timeout", type=float, default=30.0)
    revive_parser.add_argument("--test-timeout", type=float, default=60.0)
    revive_parser.add_argument("--bootstrap-pytest-requirement")
    revive_parser.add_argument("--build-isolation", action="store_true")
    revive_parser.add_argument("--max-evaluations", type=int, default=1)
    revive_parser.add_argument("--surgeon", choices=("real", "fixture"), default="fixture")
    revive_parser.add_argument(
        "--fixture",
        type=Path,
        action="append",
        help="Fixture proposal JSON; required when --surgeon fixture.",
    )

    args = parser.parse_args(argv)
    if args.command not in {"run", "revive"}:  # pragma: no cover - argparse guarantees this.
        return 2

    repository_name = args.repository.expanduser().resolve().name or "repository"
    run_root = args.workspace_root / f"{repository_name}-{uuid4().hex[:12]}"
    workspace = Workspace.create(args.repository, run_root)
    runner = PytestRunner(
        RunnerConfig(
            install_timeout_seconds=args.install_timeout,
            collection_timeout_seconds=args.collection_timeout,
            test_timeout_seconds=args.test_timeout,
            bootstrap_pytest_requirement=args.bootstrap_pytest_requirement,
            no_build_isolation=not args.build_isolation,
        )
    )
    if args.command == "revive":
        if args.surgeon == "fixture":
            if not args.fixture:
                parser.error("--fixture is required when --surgeon fixture")
            surgeon = FixtureSurgeon.from_fixtures(*args.fixture)
        else:
            surgeon = RealSurgeon()
        try:
            result = Director(
                runner=runner, config=DirectorConfig(max_evaluations=args.max_evaluations)
            ).revive(workspace, surgeon)
        except LLMClientError as error:
            print(json.dumps({"status": "llm_error", "error": str(error)}))
            return 1
        output = asdict(result)
        output["baseline_score"] = result.baseline_score.score
        output["final_score"] = result.final_score.score
        if isinstance(surgeon, RealSurgeon):
            output["model_patches"] = [patch.model_dump() for patch in surgeon.generated_patches]
        print(json.dumps(output, indent=2, default=str))
        return 0

    candidate = workspace.create_candidate()
    result = runner.run(candidate.path, candidate.artifact_dir)
    print(result.result_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())