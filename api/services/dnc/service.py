"""The suppression checks and writes that callers actually use.

Routes, the dispatcher, post-call handling and the agent tool all go through
here rather than touching the DB client directly, so normalization happens in
exactly one place. A write and a later lookup that disagreed about a number's
canonical key would produce a list that silently suppresses nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from api.db import db_client
from api.services.dnc.suppression import (
    SOURCE_AGENT,
    SOURCE_DISPOSITION,
    SOURCE_MANUAL,
    disposition_requests_suppression,
    normalize_dnc_number,
    normalize_dnc_numbers,
)


@dataclass
class DNCAddResult:
    """What an add or import actually did."""

    added: int = 0
    already_listed: int = 0
    invalid: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return self.added + self.already_listed


class DNCService:
    async def add_numbers(
        self,
        *,
        organization_id: int,
        raw_numbers: Sequence[object],
        source: str = SOURCE_MANUAL,
        reason: str | None = None,
        created_by: int | None = None,
        expires_at: datetime | None = None,
        workflow_run_id: int | None = None,
    ) -> DNCAddResult:
        """Add numbers to an organization's list.

        Unparseable rows are returned rather than raising: an import of ten
        thousand numbers should not fail because one cell holds a note.
        """
        accepted, rejected = normalize_dnc_numbers(raw_numbers)
        if not accepted:
            return DNCAddResult(invalid=rejected)

        # Keep the first raw spelling of each accepted key, so an operator
        # auditing the list can see what was actually submitted.
        raw_by_key: dict[str, str] = {}
        for raw in raw_numbers:
            key = normalize_dnc_number(raw)
            if key is not None and key not in raw_by_key:
                raw_by_key[key] = str(raw).strip()

        added = await db_client.add_dnc_entries(
            organization_id=organization_id,
            phone_numbers=accepted,
            source=source,
            reason=reason,
            created_by=created_by,
            expires_at=expires_at,
            workflow_run_id=workflow_run_id,
            raw_inputs=[raw_by_key.get(key) for key in accepted],
        )
        return DNCAddResult(
            added=added,
            already_listed=len(accepted) - added,
            invalid=rejected,
        )

    async def remove_number(self, *, organization_id: int, raw_number: object) -> bool:
        key = normalize_dnc_number(raw_number)
        if key is None:
            return False
        return await db_client.remove_dnc_entry(
            organization_id=organization_id, phone_number=key
        )

    async def is_suppressed(self, *, organization_id: int, raw_number: object) -> bool:
        """Whether one number may not be called.

        A number that cannot be parsed is *not* treated as suppressed — it
        would never have matched a stored key either, and refusing to dial
        everything unparseable would silently kill campaigns whose lists use a
        format this doesn't recognise.
        """
        key = normalize_dnc_number(raw_number)
        if key is None:
            return False
        return await db_client.is_number_suppressed(
            organization_id=organization_id, phone_number=key
        )

    async def partition_suppressed(
        self, *, organization_id: int, raw_numbers: Iterable[object]
    ) -> set[str]:
        """Return the raw inputs from this batch that are suppressed.

        Returns the caller's own spellings, not canonical keys, so a dispatch
        batch can match them straight back to its leads. One query per batch.
        """
        by_key: dict[str, list[str]] = {}
        for raw in raw_numbers:
            key = normalize_dnc_number(raw)
            if key is None:
                continue
            by_key.setdefault(key, []).append(str(raw))

        if not by_key:
            return set()

        suppressed_keys = await db_client.suppressed_numbers(
            organization_id=organization_id, phone_numbers=list(by_key)
        )
        return {raw for key in suppressed_keys for raw in by_key.get(key, [])}

    async def record_disposition(
        self,
        *,
        organization_id: int,
        raw_number: object,
        disposition: object,
        workflow_run_id: int | None = None,
    ) -> bool:
        """Add a number to the list when a call ended in a DNC disposition.

        This is what turns the existing ``DNC`` disposition from a label on a
        finished call into something the next dial actually honours.
        """
        if not disposition_requests_suppression(disposition):
            return False

        result = await self.add_numbers(
            organization_id=organization_id,
            raw_numbers=[raw_number],
            source=SOURCE_DISPOSITION,
            reason=f"Call disposition {str(disposition).strip().upper()}",
            workflow_run_id=workflow_run_id,
        )
        if result.accepted:
            logger.info(
                f"Suppressed {raw_number} for org {organization_id} "
                f"from call disposition (run {workflow_run_id})"
            )
        return result.accepted > 0

    async def add_from_agent(
        self,
        *,
        organization_id: int,
        raw_number: object,
        reason: str | None = None,
        workflow_run_id: int | None = None,
    ) -> DNCAddResult:
        """Honour a caller asking, mid-call, not to be contacted again."""
        return await self.add_numbers(
            organization_id=organization_id,
            raw_numbers=[raw_number],
            source=SOURCE_AGENT,
            reason=reason or "Caller asked not to be contacted again",
            workflow_run_id=workflow_run_id,
        )


dnc_service = DNCService()
