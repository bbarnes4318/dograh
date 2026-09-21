"""CLI for the conversation eval harness.

    source venv/bin/activate && set -a && source api/.env && set +a
    python -m evals.conversation --workflow-id 42 --organization-id 1 --user-id 1

Save a baseline, change a prompt, run again against it, and the exit code says
whether anything got worse:

    python -m evals.conversation ... --save baseline.json
    python -m evals.conversation ... --baseline baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evals.conversation.personas import DEFAULT_PERSONAS, select_personas
from evals.conversation.scoring import compare_to_baseline, summarize
from evals.conversation.simulator import format_scorecard, simulate_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.conversation",
        description="Run simulated callers against a workflow and score the calls.",
    )
    parser.add_argument("--workflow-id", type=int, required=True)
    parser.add_argument("--organization-id", type=int, required=True)
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="User the eval runs are attributed to",
    )
    parser.add_argument(
        "--persona",
        action="append",
        dest="personas",
        help=(
            "Persona key to run; repeatable. Defaults to all. Available: "
            + ", ".join(p.key for p in DEFAULT_PERSONAS)
        ),
    )
    parser.add_argument(
        "--conversion-disposition",
        action="append",
        dest="conversion_dispositions",
        help="Disposition code counting as a conversion (repeatable). Default: XFER",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the rubric judge and run only the deterministic checks",
    )
    parser.add_argument("--save", type=Path, help="Write the summary JSON here")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Compare against a saved summary and fail on any regression",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of a scorecard"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    personas = select_personas(args.personas)
    results = await simulate_all(
        workflow_id=args.workflow_id,
        organization_id=args.organization_id,
        user_id=args.user_id,
        personas=personas,
        conversion_dispositions=args.conversion_dispositions or ["XFER"],
        judge=not args.no_judge,
    )
    summary = summarize(results)

    if args.save:
        args.save.write_text(json.dumps(summary, indent=2))

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_scorecard(summary))

    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        comparison = compare_to_baseline(summary, baseline)
        for improvement in comparison["improvements"]:
            print(f"  improved: {improvement['persona']} — {improvement['reason']}")
        for regression in comparison["regressions"]:
            print(f"  REGRESSED: {regression['persona']} — {regression['reason']}")
        if comparison["has_regressions"]:
            return 1

    return 0 if summary["failed"] == 0 else 1


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
