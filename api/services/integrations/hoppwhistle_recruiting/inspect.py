"""The PUBLISHED workflow as plain facts, and the runs started for a session.

Hoppwhistle compares the facts with what it compiled (its conformance check)
before every call, so this reports the stored text verbatim -- unrendered
prompts and greetings, tool names (resolved from UUIDs), extraction field
names, edges -- and judges nothing itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from api.db import db_client
from api.db.models import WorkflowModel, WorkflowRunModel


async def inspect_published_workflow(
    workflow_uuid: str, organization_id: int
) -> dict[str, Any] | None:
    workflow = await db_client.get_workflow_by_uuid(workflow_uuid, organization_id)
    if workflow is None:
        return None
    released = workflow.released_definition
    definition = (released.workflow_json if released is not None else None) or {}
    tools = await db_client.get_tools_for_organization(organization_id)
    names = {str(tool.tool_uuid): tool.name for tool in tools}
    unresolved: set[str] = set()
    nodes: list[dict[str, Any]] = []
    for node in definition.get("nodes") or []:
        data = node.get("data") or {}
        tool_names = []
        for uuid in data.get("tool_uuids") or []:
            if str(uuid) in names:
                tool_names.append(names[str(uuid)])
            else:
                unresolved.add(str(uuid))
        nodes.append(
            {
                "id": node.get("id"),
                "type": node.get("type"),
                "prompt": data.get("prompt"),
                "greeting": data.get("greeting"),
                "tool_names": tool_names,
                "extraction_variables": [
                    v.get("name")
                    for v in (data.get("extraction_variables") or [])
                    if isinstance(v, dict)
                ]
                if data.get("extraction_enabled")
                else [],
                "contract_version": data.get("recruiting_contract_version"),
                "framework_version": data.get("recruiting_framework_version"),
                "enabled": data.get("recruiting_enabled")
                if node.get("type") == "hoppwhistle_recruiting"
                else None,
            }
        )
    edges = [
        {
            "source": edge.get("source"),
            "target": edge.get("target"),
            "label": (edge.get("data") or {}).get("label"),
            "condition": (edge.get("data") or {}).get("condition"),
        }
        for edge in definition.get("edges") or []
    ]
    return {
        "workflow_uuid": str(workflow.workflow_uuid),
        "workflow_id": workflow.id,
        "published_version": released.version_number if released is not None else None,
        "nodes": nodes,
        "edges": edges,
        "unresolved_tool_uuids": sorted(unresolved),
    }


async def runs_for_session(
    session_id: str,
    organization_id: int,
    *,
    workflow_uuid: str | None = None,
    within: timedelta = timedelta(days=3),
) -> list[dict[str, Any]]:
    """Runs whose initial context names this Hoppwhistle session, in this organization only."""
    since = datetime.now(UTC) - within
    async with db_client.async_session() as session:
        query = (
            select(WorkflowRunModel)
            .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
            .where(
                WorkflowModel.organization_id == organization_id,
                WorkflowRunModel.created_at >= since,
                WorkflowRunModel.initial_context["hr_session_id"].as_string()
                == session_id,
            )
            .order_by(WorkflowRunModel.created_at.desc())
            .limit(10)
        )
        if workflow_uuid:
            query = query.where(WorkflowModel.workflow_uuid == workflow_uuid)
        rows = (await session.execute(query)).scalars().all()
    return [
        {
            "run_id": row.id,
            "state": str(getattr(row.state, "value", row.state)),
            "completed": bool(row.is_completed),
            "created_at": row.created_at.astimezone(UTC).isoformat()
            if row.created_at
            else None,
        }
        for row in rows
    ]
