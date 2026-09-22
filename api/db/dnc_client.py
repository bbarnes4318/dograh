"""Storage for do-not-call suppression entries.

Every method takes ``organization_id`` and filters on it. A suppression list
that leaked across tenants would both break isolation and reveal who a
competitor has been calling.

Numbers are stored and matched as the canonical key produced by
``api.services.dnc.normalize_dnc_number``. This client does not normalize —
callers do, so that a write and a later lookup cannot disagree about the key.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from api.db.base_client import BaseDBClient
from api.db.models import DNCEntryModel


class DNCClient(BaseDBClient):
    async def add_dnc_entries(
        self,
        *,
        organization_id: int,
        phone_numbers: Sequence[str],
        source: str = "manual",
        reason: str | None = None,
        created_by: int | None = None,
        expires_at: datetime | None = None,
        workflow_run_id: int | None = None,
        raw_inputs: Sequence[str | None] | None = None,
    ) -> int:
        """Add canonical numbers to the org's list, returning how many are new.

        Re-adding a number refreshes its reason, source and expiry rather than
        creating a duplicate: the most recent request is the one that should
        govern, and a second "stop calling me" must be able to extend a
        suppression that was about to lapse.
        """
        if not phone_numbers:
            return 0

        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for index, number in enumerate(phone_numbers):
            raw = None
            if raw_inputs is not None and index < len(raw_inputs):
                raw = raw_inputs[index]
            rows.append(
                {
                    "organization_id": organization_id,
                    "phone_number": number,
                    "raw_input": (raw or "")[:64] or None,
                    "source": source,
                    "reason": reason,
                    "created_by": created_by,
                    "expires_at": expires_at,
                    "workflow_run_id": workflow_run_id,
                    "created_at": now,
                }
            )

        statement = insert(DNCEntryModel).values(rows)
        statement = statement.on_conflict_do_update(
            constraint="uq_dnc_entries_org_number",
            set_={
                "source": statement.excluded.source,
                "reason": statement.excluded.reason,
                "expires_at": statement.excluded.expires_at,
                "raw_input": statement.excluded.raw_input,
                "workflow_run_id": statement.excluded.workflow_run_id,
            },
        ).returning(DNCEntryModel.created_at)

        async with self.async_session() as session:
            result = await session.execute(statement)
            created = result.scalars().all()
            await session.commit()

        # A row whose created_at is this batch's timestamp was inserted; one
        # carrying an older timestamp was already on the list.
        return sum(1 for created_at in created if created_at == now)

    async def suppressed_numbers(
        self, *, organization_id: int, phone_numbers: Iterable[str]
    ) -> set[str]:
        """Which of these canonical numbers are currently suppressed.

        One query for a whole dispatch batch rather than one per lead, and
        expired entries are excluded here so no caller has to remember to.
        """
        numbers = list(dict.fromkeys(phone_numbers))
        if not numbers:
            return set()

        now = datetime.now(UTC)
        async with self.async_session() as session:
            result = await session.execute(
                select(DNCEntryModel.phone_number).where(
                    DNCEntryModel.organization_id == organization_id,
                    DNCEntryModel.phone_number.in_(numbers),
                    or_(
                        DNCEntryModel.expires_at.is_(None),
                        DNCEntryModel.expires_at > now,
                    ),
                )
            )
            return set(result.scalars().all())

    async def is_number_suppressed(
        self, *, organization_id: int, phone_number: str
    ) -> bool:
        suppressed = await self.suppressed_numbers(
            organization_id=organization_id, phone_numbers=[phone_number]
        )
        return phone_number in suppressed

    async def list_dnc_entries(
        self,
        *,
        organization_id: int,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DNCEntryModel]:
        async with self.async_session() as session:
            query = select(DNCEntryModel).where(
                DNCEntryModel.organization_id == organization_id
            )
            if search:
                query = query.where(DNCEntryModel.phone_number.ilike(f"%{search}%"))
            query = (
                query.order_by(DNCEntryModel.created_at.desc(), DNCEntryModel.id.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(query)
            return list(result.scalars().all())

    async def count_dnc_entries(
        self, *, organization_id: int, search: str | None = None
    ) -> int:
        async with self.async_session() as session:
            query = select(func.count(DNCEntryModel.id)).where(
                DNCEntryModel.organization_id == organization_id
            )
            if search:
                query = query.where(DNCEntryModel.phone_number.ilike(f"%{search}%"))
            result = await session.execute(query)
            return int(result.scalar_one())

    async def remove_dnc_entry(
        self, *, organization_id: int, phone_number: str
    ) -> bool:
        """Drop one number from the list. Returns whether anything was removed."""
        async with self.async_session() as session:
            result = await session.execute(
                delete(DNCEntryModel).where(
                    DNCEntryModel.organization_id == organization_id,
                    DNCEntryModel.phone_number == phone_number,
                )
            )
            await session.commit()
            return result.rowcount > 0
