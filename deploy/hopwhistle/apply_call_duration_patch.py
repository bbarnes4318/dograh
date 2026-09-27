#!/usr/bin/env python3
"""Make hopwhistle's reports show real call durations instead of 0 seconds.

Why this exists
---------------
`hopwhistle-prod-ash` runs the upstream `dograhai/dograh-api:latest` image with
individual files bind-mounted over it from `/opt/dograh-patches/`, so fixes
merged into the fork never reach it on their own. Upstream only records a call
duration when the voice pipeline's own timer measured one; every call it did
not measure is stored (and reported) as 0 seconds, even when the carrier's
completed callback says the call was talked on for minutes.

This patch makes the carrier's duration count:

* `call_duration.py` (new, mounted at api/services/workflow/) parses carrier
  durations out of the logged status callbacks, falling back to the
  answered -> completed timestamps for carriers that send none.
* `workflow_run_client.py` (seeded from the container, then edited): every
  status callback is logged through `update_workflow_run(logs=...)`, so that
  is where the carrier duration is copied into `usage_info` whenever the
  pipeline measured nothing -- and kept when the pipeline's own usage write
  lands afterwards.

Usage (run as root on the box)
------------------------------
    # 1. see what would change, touching nothing
    python3 apply_call_duration_patch.py --dry-run

    # 2. apply: writes the files, adds the mounts to
    #    docker-compose.override.yaml, validates the compose config
    python3 apply_call_duration_patch.py

    # 3. recreate the api container so the mounts take effect.
    #    THIS DROPS CALLS IN PROGRESS -- run it between campaigns.
    python3 apply_call_duration_patch.py --restart

    # 4. repair calls already stored as 0s (report first, then write)
    python3 apply_call_duration_patch.py --backfill-since 2026-09-26
    python3 apply_call_duration_patch.py --backfill-since 2026-09-26 --backfill-apply

Nothing in workflow_run_client.py is written unless every anchor is found; a
missing anchor is reported by name and the script exits non-zero. Every file
it changes gets a `.bak-callduration-<timestamp>` copy next to it.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PATCH_DIR = Path("/opt/dograh-patches")
COMPOSE_DIR = Path("/opt/dograh")
OVERRIDE_NAME = "docker-compose.override.yaml"
BACKUP_TAG = "bak-callduration"

CLIENT_NAME = "workflow_run_client.py"
CLIENT_CONTAINER_PATH = "/app/api/db/workflow_run_client.py"
HELPER_NAME = "call_duration.py"
HELPER_CONTAINER_PATH = "/app/api/services/workflow/call_duration.py"
MOUNTS = {
    CLIENT_NAME: CLIENT_CONTAINER_PATH,
    HELPER_NAME: HELPER_CONTAINER_PATH,
}

# workflow_run_client.py is considered patched if this string is present.
CLIENT_MARKER = "telephony_duration_from_callbacks"

CLIENT_EDITS = [
    (
        "from api.services.workflow.run_usage_response import format_public_cost_info\n",
        "from api.services.workflow.call_duration import (\n"
        "    apply_telephony_duration,\n"
        "    carry_over_telephony_duration,\n"
        "    telephony_duration_from_callbacks,\n"
        ")\n"
        "from api.services.workflow.run_usage_response import format_public_cost_info\n",
    ),
    (
        "            if usage_info:\n                run.usage_info = usage_info\n",
        "            if usage_info:\n"
        "                run.usage_info = carry_over_telephony_duration(\n"
        "                    run.usage_info, usage_info\n"
        "                )\n",
    ),
    (
        "                run.logs = {**run.logs, **logs}\n",
        "                run.logs = {**run.logs, **logs}\n"
        "                # Every carrier status callback is logged through here, so\n"
        "                # this is the one place a carrier-reported duration can be\n"
        "                # turned into the run's call duration regardless of which\n"
        "                # code path handled the callback.\n"
        '                if "telephony_status_callbacks" in logs:\n'
        "                    carrier_seconds = telephony_duration_from_callbacks(\n"
        '                        run.logs.get("telephony_status_callbacks")\n'
        "                    )\n"
        "                    if carrier_seconds > 0:\n"
        "                        run.usage_info = apply_telephony_duration(\n"
        "                            run.usage_info, carrier_seconds\n"
        "                        )\n",
    ),
]

# Kept byte-identical to api/services/workflow/call_duration.py in the repo
# (api/tests/test_call_duration_box_patch.py enforces it).
CALL_DURATION_SOURCE = r'''"""Reconcile a run's call duration with the duration the carrier reports.

``usage_info.call_duration_seconds`` is normally the Pipecat pipeline's wall
time. When the pipeline never ran (the media websocket was refused or never
connected) or its timer never started, that value is missing or 0 even though
the carrier billed a connected call. The carrier's completed-status callback
carries the real talk time, so it is kept alongside as
``telephony_duration_seconds`` and used whenever the pipeline has nothing.
"""

from datetime import UTC, datetime
from typing import Any, Iterable

CALL_DURATION_KEY = "call_duration_seconds"
TELEPHONY_DURATION_KEY = "telephony_duration_seconds"
_ANSWERED_STATUSES = frozenset({"in-progress", "answered"})


def parse_duration_seconds(value: Any) -> float:
    """Parse a provider duration (``"42"``, ``42``, ``"42.5"``) into seconds."""
    if value in (None, ""):
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if parsed != parsed or parsed < 0:  # NaN or negative
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def _fill_call_duration(usage_info: dict) -> dict:
    telephony = parse_duration_seconds(usage_info.get(TELEPHONY_DURATION_KEY))
    if telephony > 0 and parse_duration_seconds(usage_info.get(CALL_DURATION_KEY)) <= 0:
        usage_info[CALL_DURATION_KEY] = telephony
    return usage_info


def apply_telephony_duration(usage_info: dict | None, seconds: Any) -> dict:
    """Return ``usage_info`` with the carrier duration recorded and, when the
    pipeline measured nothing, used as the call duration."""
    merged = dict(usage_info or {})
    telephony = parse_duration_seconds(seconds)
    if telephony <= 0:
        return merged
    merged[TELEPHONY_DURATION_KEY] = telephony
    return _fill_call_duration(merged)


def carry_over_telephony_duration(existing: dict | None, new: dict) -> dict:
    """Keep a carrier duration already stored on the run when ``new`` replaces
    ``usage_info`` wholesale (the pipeline writes its usage after, or racing
    with, the carrier's completed callback)."""
    merged = dict(new)
    if TELEPHONY_DURATION_KEY not in merged and existing:
        if TELEPHONY_DURATION_KEY in existing:
            merged[TELEPHONY_DURATION_KEY] = existing[TELEPHONY_DURATION_KEY]
    return _fill_call_duration(merged)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def telephony_duration_from_callbacks(callbacks: Iterable[Any]) -> float:
    """Call duration from the run's logged telephony status callbacks.

    Uses the largest duration a carrier reported. Carriers that report none
    on hangup (Telnyx, ARI) fall back to the time between the first answered
    callback and the completed one.
    """
    best: float = 0
    answered_at: datetime | None = None
    completed_at: datetime | None = None
    for callback in callbacks or []:
        if not isinstance(callback, dict):
            continue
        best = max(best, parse_duration_seconds(callback.get("duration")))
        status = str(callback.get("status") or "").lower()
        timestamp = _parse_timestamp(callback.get("timestamp"))
        if timestamp is None:
            continue
        if status in _ANSWERED_STATUSES and answered_at is None:
            answered_at = timestamp
        elif status == "completed" and completed_at is None:
            completed_at = timestamp

    if best > 0:
        return best
    if answered_at and completed_at and completed_at > answered_at:
        return int(round((completed_at - answered_at).total_seconds()))
    return 0
'''

# Kept byte-identical to scripts/backfill_call_durations.py in the repo.
BACKFILL_SOURCE = r'''"""Backfill call durations from the carrier's logged status callbacks.

Runs whose ``usage_info.call_duration_seconds`` is missing or 0 get the
duration the carrier reported (stored in ``logs.telephony_status_callbacks``),
the same fallback live calls now use. Runs with a non-zero pipeline duration
are left alone.

Dry run by default. Run from the repo root with the api environment loaded:

    python -m scripts.backfill_call_durations --since 2026-09-26
    python -m scripts.backfill_call_durations --since 2026-09-26 --apply
    python -m scripts.backfill_call_durations --since 2026-09-26 --campaign-id 12 --apply
"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime

from loguru import logger

from api.db import db_client
from api.services.workflow.call_duration import (
    CALL_DURATION_KEY,
    parse_duration_seconds,
    telephony_duration_from_callbacks,
)


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def backfill(
    since: datetime,
    campaign_id: int | None,
    organization_id: int | None,
    apply: bool,
) -> None:
    filters = ["wr.created_at >= :since"]
    params: dict = {"since": since}
    if campaign_id is not None:
        filters.append("wr.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if organization_id is not None:
        filters.append("w.organization_id = :organization_id")
        params["organization_id"] = organization_id

    rows = await db_client.execute_raw_query(
        f"""
        SELECT wr.id,
               wr.usage_info ->> '{CALL_DURATION_KEY}' AS call_duration,
               wr.logs -> 'telephony_status_callbacks' AS callbacks
        FROM workflow_runs wr
        JOIN workflows w ON w.id = wr.workflow_id
        WHERE {" AND ".join(filters)}
        ORDER BY wr.id
        """,
        params,
    )

    scanned = len(rows)
    already_had = 0
    no_carrier_duration = 0
    no_duration_endings: Counter[str] = Counter()
    fixed = 0
    fixed_seconds: float = 0

    for row in rows:
        if parse_duration_seconds(row["call_duration"]) > 0:
            already_had += 1
            continue
        callbacks = row["callbacks"]
        if isinstance(callbacks, str):  # raw SQL hands json back as text
            callbacks = json.loads(callbacks)
        seconds = telephony_duration_from_callbacks(callbacks or [])
        if seconds <= 0:
            no_carrier_duration += 1
            statuses = [
                str(c.get("status")) for c in callbacks or [] if isinstance(c, dict)
            ]
            no_duration_endings[statuses[-1] if statuses else "no callbacks"] += 1
            continue
        fixed += 1
        fixed_seconds += seconds
        if apply:
            # Re-logging the same callbacks runs update_workflow_run's
            # carrier-duration fill under the row lock.
            await db_client.update_workflow_run(
                run_id=row["id"], logs={"telephony_status_callbacks": callbacks}
            )

    verb = "Updated" if apply else "Would update"
    print(f"Scanned {scanned} runs created since {since.isoformat()}")
    print(f"  already had a non-zero duration: {already_had}")
    print(f"  {verb} from carrier duration:    {fixed} ({fixed_seconds / 60:.1f} min)")
    print(f"  0s with no carrier duration:     {no_carrier_duration}")
    for status, count in no_duration_endings.most_common(8):
        print(f"      last carrier status {status!r}: {count}")
    if not apply and fixed:
        print("Dry run; re-run with --apply to write.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since",
        required=True,
        help="ISO date/datetime (UTC if no offset), e.g. 2026-09-26",
    )
    parser.add_argument("--campaign-id", type=int)
    parser.add_argument("--organization-id", type=int)
    parser.add_argument("--apply", action="store_true", help="Write changes")
    args = parser.parse_args()

    logger.remove()
    asyncio.run(
        backfill(
            _parse_since(args.since),
            args.campaign_id,
            args.organization_id,
            args.apply,
        )
    )


if __name__ == "__main__":
    main()
'''


def patch_client_text(text: str) -> tuple[str | None, list[str]]:
    """Apply CLIENT_EDITS to ``text``. Returns (new text, problems)."""
    problems = []
    for index, (find, replace) in enumerate(CLIENT_EDITS, start=1):
        count = text.count(find)
        if count != 1:
            problems.append(
                f"{CLIENT_NAME}: anchor for edit {index} found {count} times, "
                f"expected once -- send me this file and I'll re-anchor it"
            )
            continue
        text = text.replace(find, replace, 1)
    return (None if problems else text), problems


def compose(compose_dir: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args], cwd=compose_dir, **kwargs)


def seed_client(patch_dir: Path, compose_dir: Path) -> bool:
    target = patch_dir / CLIENT_NAME
    if target.exists():
        return True
    result = compose(
        compose_dir,
        "exec",
        "-T",
        "api",
        "cat",
        CLIENT_CONTAINER_PATH,
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"  {CLIENT_NAME}: FAILED to read {CLIENT_CONTAINER_PATH} from the api "
            f"container\n    {result.stderr.decode(errors='replace').strip()}"
        )
        return False
    target.write_bytes(result.stdout)
    print(f"  {CLIENT_NAME}: seeded {len(result.stdout)} bytes from the container")
    return True


def backup(path: Path, stamp: str) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.{BACKUP_TAG}-{stamp}"))


def add_mounts(override_text: str, patch_dir: Path) -> tuple[str | None, list[str]]:
    """Insert the two mounts next to the existing /opt/dograh-patches mounts.

    Returns (new text or None if nothing to add, lines added). Raises
    ValueError when there is no existing patch mount to anchor on.
    """
    wanted = [
        f"{patch_dir}/{name}:{container_path}:ro"
        for name, container_path in MOUNTS.items()
    ]
    missing = [mount for mount in wanted if mount not in override_text]
    if not missing:
        return None, []

    lines = override_text.splitlines(keepends=True)
    mount_line = re.compile(
        r"^(\s*)-\s*['\"]?" + re.escape(str(patch_dir)) + r"/[^:]+:/app/"
    )
    last_index = None
    indent = ""
    for index, line in enumerate(lines):
        match = mount_line.match(line)
        if match:
            last_index = index
            indent = match.group(1)
    if last_index is None:
        raise ValueError(
            f"no existing {patch_dir}/... mount found in {OVERRIDE_NAME} to anchor on"
        )
    new_lines = [f"{indent}- {mount}\n" for mount in missing]
    if not lines[last_index].endswith("\n"):
        lines[last_index] += "\n"
    lines[last_index + 1 : last_index + 1] = new_lines
    return "".join(lines), [line.strip() for line in new_lines]


def apply(args: argparse.Namespace) -> int:
    patch_dir: Path = args.patch_dir
    compose_dir: Path = args.compose_dir
    override_path = compose_dir / OVERRIDE_NAME
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if not patch_dir.is_dir():
        print(f"{patch_dir} does not exist -- is this the hopwhistle box?")
        return 1
    if not override_path.exists():
        print(f"{override_path} does not exist -- is this the hopwhistle box?")
        return 1

    # ---- pass 1: work everything out before writing anything ----
    if not args.dry_run and not seed_client(patch_dir, compose_dir):
        return 1
    client_path = patch_dir / CLIENT_NAME
    client_plan = None
    if not client_path.exists():
        print(f"  {CLIENT_NAME}: not seeded yet (the real run seeds it)")
    else:
        text = client_path.read_text()
        if CLIENT_MARKER in text:
            print(f"  {CLIENT_NAME}: already patched")
        else:
            client_plan, problems = patch_client_text(text)
            if problems:
                print("\nNOT APPLYING ANYTHING. Problems:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1

    helper_path = patch_dir / HELPER_NAME
    helper_current = helper_path.read_text() if helper_path.exists() else None
    helper_changed = helper_current != CALL_DURATION_SOURCE

    override_text = override_path.read_text()
    try:
        override_plan, added = add_mounts(override_text, patch_dir)
    except ValueError as e:
        print(f"\nNOT APPLYING ANYTHING: {e}")
        print("Add these under the api service's volumes by hand, then re-run:")
        for name, container_path in MOUNTS.items():
            print(f"  - {patch_dir}/{name}:{container_path}:ro")
        return 1

    if args.dry_run:
        print(f"  {HELPER_NAME}: {'would write' if helper_changed else 'up to date'}")
        if client_plan:
            print(f"  {CLIENT_NAME}: would apply {len(CLIENT_EDITS)} edit(s)")
        for line in added:
            print(f"  {OVERRIDE_NAME}: would add `{line}`")
        print("\nDry run -- nothing written.")
        return 0

    # ---- pass 2: write ----
    if helper_changed:
        backup(helper_path, stamp)
        helper_path.write_text(CALL_DURATION_SOURCE)
        print(f"  {HELPER_NAME}: written")
    if client_plan:
        backup(client_path, stamp)
        client_path.write_text(client_plan)
        print(f"  {CLIENT_NAME}: applied {len(CLIENT_EDITS)} edit(s)")
    if override_plan:
        backup(override_path, stamp)
        override_path.write_text(override_plan)
        for line in added:
            print(f"  {OVERRIDE_NAME}: added `{line}`")
        check = compose(compose_dir, "config", "-q", capture_output=True)
        if check.returncode != 0:
            override_path.write_text(override_text)
            print(
                f"\n{OVERRIDE_NAME} failed `docker compose config`; restored the "
                f"original. Error:\n{check.stderr.decode(errors='replace')}"
            )
            return 1

    print(
        "\nPatched. Recreate the api container to load it (drops calls in "
        "progress):\n  python3 apply_call_duration_patch.py --restart"
    )
    return 0


def container_is_patched(compose_dir: Path) -> bool:
    check = compose(
        compose_dir,
        "exec",
        "-T",
        "api",
        "grep",
        "-q",
        CLIENT_MARKER,
        CLIENT_CONTAINER_PATH,
        capture_output=True,
    )
    return check.returncode == 0


def restart(args: argparse.Namespace) -> int:
    print("Recreating the api container (calls in progress will drop) ...")
    result = compose(
        args.compose_dir, "up", "-d", "--force-recreate", "--no-deps", "api"
    )
    if result.returncode != 0:
        return result.returncode
    if not container_is_patched(args.compose_dir):
        print("WARNING: the api container is not running the patched file yet.")
        return 1
    print("api container is running the patched file.")
    return 0


def backfill(args: argparse.Namespace) -> int:
    if args.backfill_apply and not container_is_patched(args.compose_dir):
        print(
            "The api container is not running the patch yet, so nothing would be "
            "written. Apply it and run --restart first."
        )
        return 1
    container_script = "/tmp/backfill_call_durations.py"
    source = args.patch_dir / "backfill_call_durations.py"
    source.write_text(BACKFILL_SOURCE)
    copy = compose(args.compose_dir, "cp", str(source), f"api:{container_script}")
    if copy.returncode != 0:
        return copy.returncode
    command = [
        "exec",
        "-T",
        "-w",
        "/app",
        "-e",
        "PYTHONPATH=/app",
        "api",
        "python",
        container_script,
        "--since",
        args.backfill_since,
    ]
    if args.backfill_apply:
        command.append("--apply")
    return compose(args.compose_dir, *command).returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--patch-dir", type=Path, default=PATCH_DIR)
    parser.add_argument("--compose-dir", type=Path, default=COMPOSE_DIR)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change"
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="recreate the api container (drops calls in progress)",
    )
    parser.add_argument(
        "--backfill-since",
        metavar="DATE",
        help="report 0s calls since DATE (UTC) the carrier timed",
    )
    parser.add_argument(
        "--backfill-apply",
        action="store_true",
        help="with --backfill-since, write the corrected durations",
    )
    args = parser.parse_args()

    if args.restart:
        return restart(args)
    if args.backfill_since:
        return backfill(args)
    if args.backfill_apply:
        parser.error("--backfill-apply needs --backfill-since")
    return apply(args)


if __name__ == "__main__":
    sys.exit(main())
