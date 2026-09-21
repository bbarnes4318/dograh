"""Service layer for the funnel, cohort and transcript-search reports."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from api.db import db_client
from api.services.reports.conversion_analytics import (
    DEFAULT_CONVERSION_DISPOSITIONS,
    build_conversion_cohorts,
    build_node_funnel,
)


def _utc_bounds(
    start_date: str, end_date: str, timezone: str
) -> tuple[datetime, datetime]:
    """Convert an inclusive local date range into UTC datetimes."""
    tz = ZoneInfo(timezone)
    start = datetime.combine(
        datetime.strptime(start_date, "%Y-%m-%d"), time.min, tzinfo=tz
    )
    end = datetime.combine(datetime.strptime(end_date, "%Y-%m-%d"), time.max, tzinfo=tz)
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC"))


class ConversionReportService:
    """Reports that answer where calls stop and which cohorts convert."""

    async def get_node_funnel(
        self,
        organization_id: int,
        start_date: str,
        end_date: str,
        timezone: str,
        workflow_id: Optional[int] = None,
        definition_id: Optional[int] = None,
        conversion_dispositions: Sequence[str] = DEFAULT_CONVERSION_DISPOSITIONS,
    ) -> dict[str, Any]:
        start_utc, end_utc = _utc_bounds(start_date, end_date, timezone)
        runs = await db_client.get_runs_for_conversion_analytics(
            organization_id=organization_id,
            start_utc=start_utc,
            end_utc=end_utc,
            workflow_id=workflow_id,
            definition_id=definition_id,
        )
        funnel = build_node_funnel(runs, conversion_dispositions)
        funnel.update(
            {
                "start_date": start_date,
                "end_date": end_date,
                "timezone": timezone,
                "workflow_id": workflow_id,
                "definition_id": definition_id,
            }
        )
        return funnel

    async def get_conversion_cohorts(
        self,
        organization_id: int,
        start_date: str,
        end_date: str,
        timezone: str,
        dimension: str,
        workflow_id: Optional[int] = None,
        conversion_dispositions: Sequence[str] = DEFAULT_CONVERSION_DISPOSITIONS,
    ) -> dict[str, Any]:
        start_utc, end_utc = _utc_bounds(start_date, end_date, timezone)
        runs = await db_client.get_runs_for_conversion_analytics(
            organization_id=organization_id,
            start_utc=start_utc,
            end_utc=end_utc,
            workflow_id=workflow_id,
        )
        cohorts = build_conversion_cohorts(runs, dimension, conversion_dispositions)
        cohorts.update(
            {
                "start_date": start_date,
                "end_date": end_date,
                "timezone": timezone,
                "workflow_id": workflow_id,
            }
        )
        return cohorts

    async def search_transcripts(
        self,
        organization_id: int,
        query_text: str,
        workflow_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        timezone: str = "UTC",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        start_utc = end_utc = None
        if start_date and end_date:
            start_utc, end_utc = _utc_bounds(start_date, end_date, timezone)

        results, total = await db_client.search_run_transcripts(
            organization_id=organization_id,
            query_text=query_text,
            workflow_id=workflow_id,
            start_utc=start_utc,
            end_utc=end_utc,
            limit=limit,
            offset=offset,
        )
        return {
            "query": query_text,
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": [
                {
                    "run_id": row["id"],
                    "run_name": row["name"],
                    "workflow_id": row["workflow_id"],
                    "workflow_name": row["workflow_name"],
                    "call_type": row["call_type"],
                    "created_at": row["created_at"].isoformat()
                    if row.get("created_at")
                    else None,
                    "excerpt": row["excerpt"],
                }
                for row in results
            ],
        }
