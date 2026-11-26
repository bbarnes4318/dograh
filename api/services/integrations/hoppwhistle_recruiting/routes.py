from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db.models import UserModel
from api.services.auth.depends import get_user

from .inspect import inspect_published_workflow, runs_for_session
from .installer import (
    InstallError,
    RecruitingInstallRequest,
    RecruitingWorkflowRequest,
    install_recruiting_tools,
    upsert_recruiting_workflow,
)

router = APIRouter(prefix="/integrations/hoppwhistle-recruiting", tags=["integrations"])


def _org(user: UserModel) -> int:
    if not user.selected_organization_id:
        raise HTTPException(
            status_code=400, detail="No organization selected for the user"
        )
    return int(user.selected_organization_id)


@router.post("/install")
async def install(
    request: RecruitingInstallRequest, user: UserModel = Depends(get_user)
) -> dict:
    """Install (or refresh) the recruiting tools in the caller's organization."""
    try:
        result = await install_recruiting_tools(
            request, organization_id=_org(user), user_id=int(user.id)
        )
    except InstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return asdict(result)


@router.post("/workflows")
async def upsert_workflow(
    request: RecruitingWorkflowRequest, user: UserModel = Depends(get_user)
) -> dict:
    """Create or update, and publish, a workflow Hoppwhistle compiled."""
    try:
        return await upsert_recruiting_workflow(
            request, organization_id=_org(user), user_id=int(user.id)
        )
    except InstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/workflows/{workflow_uuid}/inspect")
async def inspect(workflow_uuid: str, user: UserModel = Depends(get_user)) -> dict:
    """The published workflow's protected facts, verbatim."""
    result = await inspect_published_workflow(workflow_uuid, _org(user))
    if result is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.get("/runs")
async def runs(
    session_id: str = Query(min_length=8, max_length=100),
    workflow_uuid: Optional[str] = Query(default=None, max_length=100),
    user: UserModel = Depends(get_user),
) -> dict:
    """Runs started for a Hoppwhistle recruiting session, in this organization only."""
    return {
        "runs": await runs_for_session(
            session_id, _org(user), workflow_uuid=workflow_uuid
        )
    }
