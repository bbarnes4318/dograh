"""Report on, or switch ONE organization's / ONE workflow's STT to, AssemblyAI.

The platform rollout (ASSEMBLYAI_STT_ROLLOUT) moves only STT choices inherited
from the platform. This tool is for the rest, one explicit target at a time.

Dry run by default: prints a diff (secrets masked) and writes nothing. With
--apply it writes the change and saves the prior STT JSON to a backup file
(mode 600; it can contain the previous provider's API key) for --rollback.

Run from the repo root with the api environment loaded:

    # what the platform default reaches, and what stays explicit (read-only)
    python -m scripts.assemblyai_migrate --report

    # an organization's v2 config (BYOK pipeline mode only)
    python -m scripts.assemblyai_migrate --organization-id 7
    python -m scripts.assemblyai_migrate --organization-id 7 --apply

    # one workflow's own model configuration override (live + draft version)
    python -m scripts.assemblyai_migrate --organization-id 7 --workflow-id 42 --apply

    # undo, from the backup file --apply printed
    python -m scripts.assemblyai_migrate --rollback assemblyai-backup-org7-....json --apply

Calls in progress keep their STT; the change applies to calls that start after
it. Calls on the target need ASSEMBLYAI_API_KEY on the AI Voice runtime.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from api.services.configuration.assemblyai import platform_assemblyai_api_key
from api.services.configuration.assemblyai_migration import (
    AssemblyAIMigrationError,
    assemblyai_backup_document,
    plan_assemblyai_rollback,
    plan_assemblyai_switch,
)
from api.services.configuration.hopstt_migration import (
    HopSTTMigrationError,
    MigrationPlan,
    apply_plan,
    diff_text,
)


def _print_plan(plan: MigrationPlan) -> None:
    for change in plan.changes:
        print(diff_text(change.before, change.after, f"{change.kind} {change.id}"))
        print()


def _write_backup(plan: MigrationPlan, path: str | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = f"org{plan.organization_id}" + (
        f"-wf{plan.workflow_id}" if plan.workflow_id is not None else ""
    )
    out = Path(path or f"assemblyai-backup-{target}-{stamp}.json")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(assemblyai_backup_document(plan), fh, indent=2)
    return out


async def _report() -> int:
    from api.services.configuration.assemblyai_admin import rollout_report

    report = await rollout_report()
    print(f"rollout: {report['rollout']}")
    print(f"organizations: {report['organizations_by_classification']}")
    print(f"workflows:     {report['workflows_by_classification']}")
    print()
    print("ORG    CLASSIFICATION        CURRENT STT                       MOVES NOW")
    for org in report["organizations"]:
        stt = org.get("current_stt") or {}
        print(
            f"{org['organization_id']:<6} {org.get('classification', ''):<21} "
            f"{(stt.get('provider') or '-') + '/' + str(stt.get('model') or '-'):<33} "
            f"{'yes' if org.get('moves_under_current_rollout') else 'no'}"
        )
    print()
    print("Workflows with their own STT (kept as is):")
    for wf in report["workflows"]:
        if wf["classification"] == "workflow_override":
            stt = wf.get("current_stt") or {}
            print(
                f"  wf {wf['workflow_id']} org {wf['organization_id']} "
                f"{wf['name']!r}: {stt.get('provider')}/{stt.get('model')}"
            )
    print()
    print(report["note"])
    return 0


async def run(args) -> int:
    if args.report:
        return await _report()
    try:
        if args.rollback:
            backup = json.loads(Path(args.rollback).read_text())
            plan = await plan_assemblyai_rollback(backup, force=args.force)
        else:
            if args.organization_id is None:
                raise AssemblyAIMigrationError("--organization-id is required")
            plan = await plan_assemblyai_switch(
                organization_id=args.organization_id,
                workflow_id=args.workflow_id,
                model=args.model,
                language=args.language,
                include_byok=args.include_byok,
            )
    except HopSTTMigrationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    _print_plan(plan)
    if not args.apply:
        print("Dry run; re-run with --apply to write.")
        return 0

    backup_path = None
    if not args.rollback:
        backup_path = _write_backup(plan, args.backup_file)
    await apply_plan(plan)
    verb = "Rolled back" if args.rollback else "Switched"
    print(
        f"{verb} {len(plan.changes)} row(s). Calls already in progress are unaffected."
    )
    if backup_path:
        print(
            f"Prior STT saved to {backup_path} (may contain credentials; keep private)."
        )
        print(
            f"Undo with: python -m scripts.assemblyai_migrate --rollback {backup_path} --apply"
        )
    if not args.rollback and not platform_assemblyai_api_key():
        print(
            "WARNING: ASSEMBLYAI_API_KEY is not set in this environment. Calls on the "
            "target will be refused until the AI Voice runtime has the key."
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report", action="store_true", help="read-only rollout report"
    )
    parser.add_argument("--organization-id", type=int)
    parser.add_argument("--workflow-id", type=int)
    parser.add_argument("--model", help="default assemblyai-stt-3-7-realtime-agent")
    parser.add_argument(
        "--language", choices=["en", "es", "multi", "de", "fr", "pt", "it"]
    )
    parser.add_argument(
        "--include-byok",
        action="store_true",
        help="also switch an org that uses its own STT key (tenant agreement required)",
    )
    parser.add_argument("--rollback", metavar="BACKUP_JSON")
    parser.add_argument(
        "--force", action="store_true", help="rollback even if STT changed since"
    )
    parser.add_argument("--backup-file", help="where --apply saves the prior STT JSON")
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()
    logger.remove()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
