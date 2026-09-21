from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import String, and_, func, select

from api.db.base_client import BaseDBClient
from api.db.models import WorkflowModel, WorkflowRunModel

# Ceiling on how many runs one funnel/cohort report will load. Each row
# carries three JSON columns, so an unbounded date range is a memory risk in
# the API worker, not just a slow query.
MAX_CONVERSION_ANALYTICS_RUNS = 50_000


class ReportsClient(BaseDBClient):
    async def search_run_transcripts(
        self,
        organization_id: int,
        query_text: str,
        workflow_id: Optional[int] = None,
        start_utc: Optional[datetime] = None,
        end_utc: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Full-text search over persisted call transcripts.

        Uses ``websearch_to_tsquery`` so callers can type what they'd type into
        a search box — quoted phrases, ``or``, leading ``-`` to exclude — rather
        than tsquery syntax. Matches are ranked, and a headline gives the
        surrounding words so a result is readable without opening the call.
        """
        tsvector = func.to_tsvector(
            "english", func.coalesce(WorkflowRunModel.transcript_text, "")
        )
        tsquery = func.websearch_to_tsquery("english", query_text)

        conditions = [
            WorkflowModel.organization_id == organization_id,
            tsvector.op("@@")(tsquery),
        ]
        if workflow_id is not None:
            conditions.append(WorkflowRunModel.workflow_id == workflow_id)
        if start_utc is not None:
            conditions.append(WorkflowRunModel.created_at >= start_utc)
        if end_utc is not None:
            conditions.append(WorkflowRunModel.created_at <= end_utc)

        async with self.async_session() as session:
            count_query = (
                select(func.count(WorkflowRunModel.id))
                .select_from(WorkflowRunModel)
                .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
                .where(and_(*conditions))
            )
            total = (await session.execute(count_query)).scalar() or 0

            rank = func.ts_rank(tsvector, tsquery)
            query = (
                select(
                    WorkflowRunModel.id,
                    WorkflowRunModel.name,
                    WorkflowRunModel.workflow_id,
                    WorkflowRunModel.created_at,
                    WorkflowRunModel.call_type,
                    WorkflowModel.name.label("workflow_name"),
                    func.ts_headline(
                        "english",
                        func.coalesce(WorkflowRunModel.transcript_text, ""),
                        tsquery,
                        "MaxFragments=2, MinWords=8, MaxWords=25",
                    ).label("excerpt"),
                    rank.label("rank"),
                )
                .select_from(WorkflowRunModel)
                .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
                .where(and_(*conditions))
                .order_by(rank.desc(), WorkflowRunModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(query)
            return [dict(row._mapping) for row in result], total

    async def get_runs_for_conversion_analytics(
        self,
        organization_id: int,
        start_utc: datetime,
        end_utc: datetime,
        workflow_id: Optional[int] = None,
        definition_id: Optional[int] = None,
        limit: int = MAX_CONVERSION_ANALYTICS_RUNS,
    ) -> List[Dict[str, Any]]:
        """Fetch the fields the funnel and cohort reports read.

        Deliberately does not select ``logs`` — the per-call event blob is the
        largest column on the table and none of this needs it: the node path
        and response metrics are summarized onto ``gathered_context`` and
        ``usage_info`` when the call ends.

        Still bounded, though. The date range comes from the caller, and three
        JSON columns per run across an unbounded range is enough to pull a very
        large result set into this process — a wide enough range would take the
        API worker down rather than return a slow report. ``limit`` caps it at
        the most recent runs in range, and a run that hits the cap is logged so
        a truncated report is visible rather than silently wrong.
        """
        async with self.async_session() as session:
            query = (
                select(
                    WorkflowRunModel.id,
                    WorkflowRunModel.workflow_id,
                    WorkflowRunModel.definition_id,
                    WorkflowRunModel.created_at,
                    WorkflowRunModel.call_type,
                    WorkflowRunModel.gathered_context,
                    WorkflowRunModel.initial_context,
                    WorkflowRunModel.usage_info,
                )
                .select_from(WorkflowRunModel)
                .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
                .where(
                    and_(
                        WorkflowModel.organization_id == organization_id,
                        WorkflowRunModel.created_at >= start_utc,
                        WorkflowRunModel.created_at <= end_utc,
                    )
                )
            )

            if workflow_id is not None:
                query = query.where(WorkflowRunModel.workflow_id == workflow_id)
            if definition_id is not None:
                query = query.where(WorkflowRunModel.definition_id == definition_id)

            # Newest first, so a range that exceeds the cap keeps the runs an
            # operator is most likely asking about.
            query = query.order_by(WorkflowRunModel.created_at.desc()).limit(limit)

            result = await session.execute(query)
            rows = [dict(row._mapping) for row in result]
            if len(rows) >= limit:
                logger.warning(
                    f"Conversion analytics hit the {limit}-run cap for org "
                    f"{organization_id} between {start_utc} and {end_utc}; "
                    "the report covers the most recent runs only."
                )
            return rows

    async def get_workflow_runs_for_daily_report(
        self,
        organization_id: int,
        start_utc: datetime,
        end_utc: datetime,
        workflow_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Optimized method for daily reports - fetches only required JSON fields.
        Uses PostgreSQL JSON operators to extract only needed fields from JSON columns.

        Args:
            organization_id: The organization ID to filter by
            start_utc: Start datetime in UTC
            end_utc: End datetime in UTC
            workflow_id: Optional workflow ID to filter by

        Returns:
            List of dictionaries with report-specific fields
        """
        async with self.async_session() as session:
            # Select only the specific JSON fields needed for daily reports
            # Using PostgreSQL's JSON operators to extract specific fields
            query = (
                select(
                    WorkflowRunModel.id,
                    WorkflowRunModel.workflow_id,
                    WorkflowRunModel.created_at,
                    WorkflowRunModel.call_type,
                    # Extract only specific fields from JSON columns
                    # Use TRIM and REPLACE to remove any quotes from JSON values
                    func.coalesce(
                        func.replace(
                            func.replace(
                                func.cast(
                                    WorkflowRunModel.gathered_context[
                                        "mapped_call_disposition"
                                    ],
                                    String,
                                ),
                                '"',
                                "",
                            ),
                            "'",
                            "",
                        ),
                        "UNKNOWN",
                    ).label("disposition"),
                    func.coalesce(
                        func.replace(
                            func.replace(
                                func.cast(
                                    WorkflowRunModel.gathered_context[
                                        "customer_phone_number"
                                    ],
                                    String,
                                ),
                                '"',
                                "",
                            ),
                            "'",
                            "",
                        ),
                        func.replace(
                            func.replace(
                                func.cast(
                                    WorkflowRunModel.initial_context["phone_number"],
                                    String,
                                ),
                                '"',
                                "",
                            ),
                            "'",
                            "",
                        ),
                        "",
                    ).label("phone_number"),
                    func.coalesce(
                        func.replace(
                            func.replace(
                                func.cast(
                                    WorkflowRunModel.usage_info[
                                        "call_duration_seconds"
                                    ],
                                    String,
                                ),
                                '"',
                                "",
                            ),
                            "'",
                            "",
                        ),
                        "0",
                    ).label("call_duration_seconds"),
                    WorkflowModel.name.label("workflow_name"),
                )
                .select_from(WorkflowRunModel)
                .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
                .where(
                    and_(
                        WorkflowModel.organization_id == organization_id,
                        WorkflowRunModel.created_at >= start_utc,
                        WorkflowRunModel.created_at <= end_utc,
                    )
                )
            )

            if workflow_id is not None:
                query = query.where(WorkflowRunModel.workflow_id == workflow_id)

            result = await session.execute(query)
            rows = result.all()

            return [
                {
                    "id": row.id,
                    "workflow_id": row.workflow_id,
                    "workflow_name": row.workflow_name,
                    "created_at": row.created_at,
                    "call_type": row.call_type,
                    "gathered_context": {
                        "mapped_call_disposition": row.disposition,
                        "customer_phone_number": row.phone_number,  # Also provide it here for compatibility
                    },
                    "usage_info": {"call_duration_seconds": row.call_duration_seconds},
                    "initial_context": {"phone_number": row.phone_number},
                }
                for row in rows
            ]

    async def get_workflows_for_organization(
        self, organization_id: int
    ) -> List[WorkflowModel]:
        """
        Get all workflows for an organization.

        Args:
            organization_id: The organization ID
        """
        async with self.async_session() as session:
            query = (
                select(WorkflowModel)
                .where(WorkflowModel.organization_id == organization_id)
                .order_by(WorkflowModel.name)
            )

            result = await session.execute(query)
            return result.scalars().all()
