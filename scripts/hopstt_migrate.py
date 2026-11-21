"""Switch ONE organization's or ONE workflow's STT to HopSTT, or roll it back.

Dry run by default: prints a diff (secrets masked) and writes nothing. With
--apply it writes the change and saves the prior STT JSON to a backup file
(mode 600; it contains the previous provider's API key) for --rollback.

Run from the repo root with the api environment loaded:

    # an organization's v2 config (BYOK pipeline mode only)
    python -m scripts.hopstt_migrate --organization-id 7
    python -m scripts.hopstt_migrate --organization-id 7 --apply

    # one workflow's own model configuration override (live + draft version)
    python -m scripts.hopstt_migrate --organization-id 7 --workflow-id 42 \\
        --model nemotron-3.5-streaming --language es --apply

    # undo, from the backup file --apply printed
    python -m scripts.hopstt_migrate --rollback hopstt-backup-org7-....json
    python -m scripts.hopstt_migrate --rollback hopstt-backup-org7-....json --apply

The runtime needs HOPSTT_ENABLED=true (and the endpoint/key, or
HOPSTT_DEFAULT_ENDPOINT / HOPSTT_API_KEY) before calls on the target can start.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from api.services.configuration.hopstt import hopstt_enabled
from api.services.configuration.hopstt_migration import (
    HopSTTMigrationError,
    MigrationPlan,
    apply_plan,
    backup_document,
    diff_text,
    plan_rollback,
    plan_switch,
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
    out = Path(path or f"hopstt-backup-{target}-{stamp}.json")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(backup_document(plan), fh, indent=2)
    return out


async def run(args) -> int:
    try:
        if args.rollback:
            backup = json.loads(Path(args.rollback).read_text())
            if backup.get("kind") != "hopstt_migration_backup":
                raise HopSTTMigrationError("not a hopstt migration backup file")
            plan = await plan_rollback(backup, force=args.force)
        else:
            if args.organization_id is None:
                raise HopSTTMigrationError("--organization-id is required")
            plan = await plan_switch(
                organization_id=args.organization_id,
                workflow_id=args.workflow_id,
                endpoint=args.endpoint,
                model=args.model,
                language=args.language,
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
    print(f"{verb} {len(plan.changes)} row(s).")
    if backup_path:
        print(f"Prior STT saved to {backup_path} (contains credentials; keep private).")
        print(
            f"Undo with: python -m scripts.hopstt_migrate --rollback {backup_path} --apply"
        )
    if not args.rollback and not hopstt_enabled():
        print(
            "WARNING: HOPSTT_ENABLED is not true in this environment. Calls on the "
            "target will fail to start until the AI Voice runtime enables HopSTT."
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--organization-id", type=int)
    parser.add_argument("--workflow-id", type=int)
    parser.add_argument(
        "--endpoint", help="HopSTT endpoint(s); default: runtime default"
    )
    parser.add_argument("--model", help="default nemotron-3.5-streaming")
    parser.add_argument("--language", choices=["en", "es", "auto"])
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
