"""Managing inbound routing groups: validation, audit and stats.

The call-time resolver is in ``inbound_routing.py``; this module backs the
``/inbound-routing-groups`` API.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from api.db import db_client
from api.db.models import InboundRoutingGroupModel
from api.enums import WorkflowStatus
from api.schemas.inbound_routing import (
    InboundRoutingGroupResponse,
    InboundRoutingStatsResponse,
    LinkedAgent,
    LinkedWorkspaceAgents,
    RoutingTarget,
    RoutingTargetResponse,
    RoutingTargetStats,
)
from api.services.telephony.linked_workspaces import (
    LinkedWorkspace,
    linked_organization_ids,
    linked_workspaces,
)


class RoutingGroupValidationError(Exception):
    """A request references something outside the caller's org."""


async def _workflow_owners(
    workflow_ids: Iterable[int], organization_id: int
) -> Dict[int, Tuple[str, Optional[LinkedWorkspace]]]:
    """``{workflow_id: (name, linked workspace or None)}`` for workflows in
    this org or one of its linked workspaces (linked_workspaces.py)."""
    remaining = sorted(set(workflow_ids))
    if not remaining:
        return {}
    found: Dict[int, Tuple[str, Optional[LinkedWorkspace]]] = {}
    for w in await db_client.get_workflows_by_ids(remaining, organization_id):
        found[w.id] = (w.name, None)
    for link in await linked_workspaces(organization_id):
        remaining = [i for i in remaining if i not in found]
        if not remaining:
            break
        for w in await db_client.get_workflows_by_ids(remaining, link.organization_id):
            found[w.id] = (w.name, link)
    return found


async def workflow_names(
    workflow_ids: Iterable[int], organization_id: int
) -> Dict[int, str]:
    """Names of workflows in this org or its linked workspaces; a linked
    workspace's agent is shown with the workspace's label."""
    return {
        wid: (f"{name} ({link.label})" if link else name)
        for wid, (name, link) in (
            await _workflow_owners(workflow_ids, organization_id)
        ).items()
    }


async def validate_target_workflows(
    targets: List[RoutingTarget], organization_id: int
) -> None:
    """Every target workflow must belong to the org or one of the workspaces
    it has linked (FK alone only proves the row exists)."""
    ids = {t.workflow_id for t in targets}
    found = await _workflow_owners(ids, organization_id)
    missing = sorted(ids - set(found))
    if missing:
        raise RoutingGroupValidationError(
            f"Workflow(s) not found in this organization or its linked "
            f"workspaces: {', '.join(str(i) for i in missing)}"
        )


async def linked_workspace_agents(organization_id: int) -> List[LinkedWorkspaceAgents]:
    """Active agents of each linked workspace, for the routing editor."""
    result: List[LinkedWorkspaceAgents] = []
    for link in await linked_workspaces(organization_id):
        workflows = await db_client.get_all_workflows_for_listing(
            organization_id=link.organization_id,
            status=WorkflowStatus.ACTIVE.value,
        )
        result.append(
            LinkedWorkspaceAgents(
                organization_id=link.organization_id,
                label=link.label,
                agents=[
                    LinkedAgent(workflow_id=w.id, name=w.name)
                    for w in sorted(workflows, key=lambda w: (w.name or "").lower())
                ],
            )
        )
    return result


async def validate_configurations(
    telephony_configuration_ids: List[int], organization_id: int
) -> None:
    if not telephony_configuration_ids:
        return
    configs = await db_client.list_telephony_configurations(organization_id)
    owned = {c.id for c in configs}
    missing = sorted(set(telephony_configuration_ids) - owned)
    if missing:
        raise RoutingGroupValidationError(
            f"Telephony configuration(s) not found in this organization: "
            f"{', '.join(str(i) for i in missing)}"
        )


def describe_split(group: InboundRoutingGroupModel) -> Dict[str, int]:
    return {str(t.workflow_id): t.weight for t in group.targets}


async def group_responses(
    groups: List[InboundRoutingGroupModel], organization_id: int
) -> List[InboundRoutingGroupResponse]:
    counts = await db_client.count_numbers_by_inbound_routing_group(organization_id)
    names = await workflow_names(
        (t.workflow_id for g in groups for t in g.targets), organization_id
    )
    return [
        InboundRoutingGroupResponse(
            id=g.id,
            name=g.name,
            is_active=g.is_active,
            sticky_by_caller=g.sticky_by_caller,
            number_count=counts.get(g.id, 0),
            targets=[
                RoutingTargetResponse(
                    workflow_id=t.workflow_id,
                    workflow_name=names.get(t.workflow_id),
                    weight=t.weight,
                )
                for t in g.targets
            ],
            created_at=g.created_at,
            updated_at=g.updated_at,
        )
        for g in groups
    ]


async def record_audit(
    organization_id: int,
    group_id: Optional[int],
    user_id: Optional[int],
    action: str,
    details: Dict[str, Any],
) -> None:
    await db_client.add_inbound_routing_audit(
        organization_id=organization_id,
        group_id=group_id,
        user_id=user_id,
        action=action,
        details=details,
    )


def compute_stats(
    group_id: int,
    hours: int,
    since: datetime,
    configured: Dict[int, int],
    calls_by_workflow: Dict[int, int],
    names: Dict[int, str],
) -> InboundRoutingStatsResponse:
    total = sum(calls_by_workflow.values())
    rows: List[RoutingTargetStats] = []
    for workflow_id in sorted(set(configured) | set(calls_by_workflow)):
        calls = calls_by_workflow.get(workflow_id, 0)
        rows.append(
            RoutingTargetStats(
                workflow_id=workflow_id,
                workflow_name=names.get(workflow_id),
                calls=calls,
                actual_percent=round(100.0 * calls / total, 1) if total else 0.0,
                target_percent=configured.get(workflow_id, 0),
                is_target=workflow_id in configured,
            )
        )
    return InboundRoutingStatsResponse(
        group_id=group_id, hours=hours, since=since, total_calls=total, targets=rows
    )


async def group_stats(
    group: InboundRoutingGroupModel, organization_id: int, hours: int
) -> InboundRoutingStatsResponse:
    since = datetime.now(UTC) - timedelta(hours=hours)
    # Calls a split sent to a linked workspace's agent are that workspace's
    # runs, so count across the org and its linked workspaces.
    calls = await db_client.count_inbound_runs_by_workflow_for_routing_group(
        group.id,
        [organization_id, *await linked_organization_ids(organization_id)],
        since,
    )
    configured = {t.workflow_id: t.weight for t in group.targets}
    names = await workflow_names(set(configured) | set(calls), organization_id)
    return compute_stats(group.id, hours, since, configured, calls, names)
