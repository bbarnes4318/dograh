"""Backfill call durations from the carrier's logged status callbacks.

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
