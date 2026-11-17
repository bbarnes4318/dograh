"""Install the Holiday Voices agents from the server, without an API key.

    python -m api.services.integrations.holiday_voices.cli \\
        --provider-id hoppwhistle-holiday-voices \\
        --base-url https://agents.example.com [--engine-key hvk_...] [--refresh-workflows]

Run inside the voice app's API container by Hoppwhistle's
``scripts/holiday-voices-setup.sh``. The organization is the one selected by
the user whose ``provider_id`` the setup script created for the store (the
same row Hoppwhistle's SSO signs in as), so the install can only land in the
store's own workspace. The engine key may also come from ``HV_ENGINE_KEY`` so
it never appears in the process list. Prints the result as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict


async def _main(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from api.db import db_client
    from api.db.models import UserModel

    from .installer import (
        HolidayVoicesInstallRequest,
        InstallError,
        install_holiday_voices,
    )

    async with db_client.async_session() as session:
        user = (
            await session.execute(
                select(UserModel).where(UserModel.provider_id == args.provider_id)
            )
        ).scalar_one_or_none()
    if user is None or not user.selected_organization_id:
        print(
            json.dumps(
                {"error": f"No voice user with provider_id {args.provider_id!r}"}
            ),
            file=sys.stderr,
        )
        return 2
    try:
        request = HolidayVoicesInstallRequest(
            base_url=args.base_url,
            engine_key=args.engine_key or os.environ.get("HV_ENGINE_KEY") or None,
            refresh_workflows=args.refresh_workflows,
            create_workflows=not args.tools_only,
            cost_per_minute_cents=args.cost_per_minute_cents,
        )
        result = await install_holiday_voices(
            request,
            organization_id=int(user.selected_organization_id),
            user_id=int(user.id),
        )
    except (InstallError, ValueError) as exc:
        print(json.dumps({"error": getattr(exc, "message", str(exc))}), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--engine-key")
    parser.add_argument("--refresh-workflows", action="store_true")
    parser.add_argument("--tools-only", action="store_true")
    parser.add_argument("--cost-per-minute-cents", type=float)
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
