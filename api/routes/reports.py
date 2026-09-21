from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.reports import ConversionReportService, DailyReportService
from api.services.reports.conversion_analytics import (
    COHORT_DIMENSIONS,
    DEFAULT_CONVERSION_DISPOSITIONS,
)

router = APIRouter(prefix="/organizations/reports")


class DailyReportResponse(BaseModel):
    date: str
    timezone: str
    workflow_id: Optional[int]
    metrics: Dict[str, int]
    disposition_distribution: List[Dict[str, Any]]
    call_duration_distribution: List[Dict[str, Any]]


class WorkflowOption(BaseModel):
    id: int
    name: str


class WorkflowRunDetail(BaseModel):
    phone_number: str
    disposition: str
    duration_seconds: float
    workflow_id: int
    run_id: int
    workflow_name: str
    created_at: str
    call_type: str


@router.get("/daily", response_model=DailyReportResponse)
async def get_daily_report(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(..., description="IANA timezone (e.g., 'America/New_York')"),
    workflow_id: Optional[int] = Query(
        None, description="Optional workflow ID to filter by"
    ),
    user: UserModel = Depends(get_user),
) -> DailyReportResponse:
    """
    Get daily report for the specified date and timezone.
    If workflow_id is provided, filters results to that specific workflow.
    If workflow_id is None, includes all workflows for the organization.
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )

    report_service = DailyReportService()

    try:
        report = await report_service.get_daily_report(
            organization_id=user.selected_organization_id,
            date=date,
            timezone=timezone,
            workflow_id=workflow_id,
        )
        return DailyReportResponse(**report)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows", response_model=List[WorkflowOption])
async def get_workflow_options(
    user: UserModel = Depends(get_user),
) -> List[WorkflowOption]:
    """
    Get all workflows for the user's organization.
    Used to populate the workflow selector dropdown in the reports page.
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    report_service = DailyReportService()

    workflows = await report_service.get_workflows_for_organization(
        organization_id=user.selected_organization_id
    )

    return [WorkflowOption(**w) for w in workflows]


@router.get("/daily/runs", response_model=List[WorkflowRunDetail])
async def get_daily_runs_detail(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(..., description="IANA timezone (e.g., 'America/New_York')"),
    workflow_id: Optional[int] = Query(
        None, description="Optional workflow ID to filter by"
    ),
    user: UserModel = Depends(get_user),
) -> List[WorkflowRunDetail]:
    """
    Get detailed workflow runs for the specified date.
    Used for CSV export functionality.
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )

    report_service = DailyReportService()

    try:
        runs = await report_service.get_daily_runs_detail(
            organization_id=user.selected_organization_id,
            date=date,
            timezone=timezone,
            workflow_id=workflow_id,
        )
        return [WorkflowRunDetail(**run) for run in runs]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class FunnelStep(BaseModel):
    node_id: str
    node_name: str
    reached: int
    reached_pct_of_runs: float
    dropped_from_previous: int
    drop_off_pct_from_previous: float
    converted: int
    conversion_pct_of_reached: float


class NodeFunnelResponse(BaseModel):
    start_date: str
    end_date: str
    timezone: str
    workflow_id: Optional[int]
    definition_id: Optional[int]
    total_runs: int
    runs_with_node_path: int
    conversion_dispositions: List[str]
    steps: List[FunnelStep]


class CohortRow(BaseModel):
    cohort: str
    runs: int
    converted: int
    conversion_pct: float
    avg_p95_latency_seconds: Optional[float]
    runs_with_latency: int


class ConversionCohortsResponse(BaseModel):
    start_date: str
    end_date: str
    timezone: str
    workflow_id: Optional[int]
    dimension: str
    conversion_dispositions: List[str]
    cohorts: List[CohortRow]


class TranscriptSearchResult(BaseModel):
    run_id: int
    run_name: str
    workflow_id: int
    workflow_name: str
    call_type: str
    created_at: Optional[str]
    excerpt: str


class TranscriptSearchResponse(BaseModel):
    query: str
    total: int
    limit: int
    offset: int
    results: List[TranscriptSearchResult]


def _require_organization(user: UserModel) -> int:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    return user.selected_organization_id


def _validate_date_range(start_date: str, end_date: str) -> None:
    for value in (start_date, end_date):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
            )
    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must not be after end_date"
        )


def _parse_conversion_dispositions(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(DEFAULT_CONVERSION_DISPOSITIONS)
    codes = [code.strip() for code in raw.split(",") if code.strip()]
    if not codes:
        raise HTTPException(
            status_code=400, detail="conversion_dispositions must not be empty"
        )
    return codes


@router.get("/funnel", response_model=NodeFunnelResponse)
async def get_node_funnel(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
    timezone: str = Query("UTC", description="IANA timezone the dates are in"),
    workflow_id: Optional[int] = Query(None, description="Filter to one workflow"),
    definition_id: Optional[int] = Query(
        None, description="Filter to one workflow version"
    ),
    conversion_dispositions: Optional[str] = Query(
        None,
        description=(
            "Comma-separated disposition codes that count as a conversion. "
            "Defaults to XFER."
        ),
    ),
    user: UserModel = Depends(get_user),
) -> NodeFunnelResponse:
    """How far calls get through the workflow, and where they stop."""
    organization_id = _require_organization(user)
    _validate_date_range(start_date, end_date)

    try:
        funnel = await ConversionReportService().get_node_funnel(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            workflow_id=workflow_id,
            definition_id=definition_id,
            conversion_dispositions=_parse_conversion_dispositions(
                conversion_dispositions
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return NodeFunnelResponse(**funnel)


@router.get("/cohorts", response_model=ConversionCohortsResponse)
async def get_conversion_cohorts(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
    dimension: str = Query(..., description=f"One of: {', '.join(COHORT_DIMENSIONS)}"),
    timezone: str = Query("UTC", description="IANA timezone the dates are in"),
    workflow_id: Optional[int] = Query(None, description="Filter to one workflow"),
    conversion_dispositions: Optional[str] = Query(
        None,
        description=(
            "Comma-separated disposition codes that count as a conversion. "
            "Defaults to XFER."
        ),
    ),
    user: UserModel = Depends(get_user),
) -> ConversionCohortsResponse:
    """Conversion rate split by version, model, daypart and the like."""
    organization_id = _require_organization(user)
    _validate_date_range(start_date, end_date)

    if dimension not in COHORT_DIMENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"dimension must be one of: {', '.join(COHORT_DIMENSIONS)}",
        )

    try:
        cohorts = await ConversionReportService().get_conversion_cohorts(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            dimension=dimension,
            workflow_id=workflow_id,
            conversion_dispositions=_parse_conversion_dispositions(
                conversion_dispositions
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return ConversionCohortsResponse(**cohorts)


@router.get("/transcripts/search", response_model=TranscriptSearchResponse)
async def search_transcripts(
    q: str = Query(..., min_length=2, description="Search text, e.g. 'too expensive'"),
    workflow_id: Optional[int] = Query(None, description="Filter to one workflow"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    timezone: str = Query("UTC", description="IANA timezone the dates are in"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_user),
) -> TranscriptSearchResponse:
    """Find calls by what was said on them."""
    organization_id = _require_organization(user)
    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status_code=400, detail="Provide both start_date and end_date, or neither"
        )
    if start_date and end_date:
        _validate_date_range(start_date, end_date)

    try:
        found = await ConversionReportService().search_transcripts(
            organization_id=organization_id,
            query_text=q,
            workflow_id=workflow_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TranscriptSearchResponse(**found)
