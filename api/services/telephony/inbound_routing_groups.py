"""Managing inbound routing groups: validation, audit and stats.

The call-time resolver is in ``inbound_routing.py``; this module backs the
``/inbound-routing-groups`` API.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from api.db import db_client
from api.db.models import InboundRoutingGroupModel
from api.enums import WorkflowStatus
from api.schemas.inbound_routing import (
    InboundRoutingGroupResponse,
    InboundRoutingStatsResponse,
    LinkedAgent,
    LinkedWorkspaceAgents,
    RoutingRefusal,
    RoutingTarget,
    RoutingTargetResponse,
    RoutingTargetStats,
)
from api.services.telephony.linked_workspaces import (
    LinkedWorkspace,
    linked_organization_ids,
    linked_workspaces,
    self_label,
)

# Audit action for a call the group routed that was then refused before an
# agent answered (concurrency limit, quota, workspace gone): no run exists for
# it, so this row is the only record. Stats count these.
CALL_REFUSED_ACTION = "call_refused"


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


async def target_details(
    workflow_ids: Iterable[int], organization_id: int
) -> Dict[int, Dict[str, Any]]:
    """Per workflow: its own name, the workspace it runs in and that
    workspace's label (the org's ``self_label`` for its own agents)."""
    owners = await _workflow_owners(workflow_ids, organization_id)
    own_label = await self_label(organization_id) if owners else None
    return {
        wid: {
            "agent_name": name,
            "organization_id": link.organization_id if link else organization_id,
            "organization_label": link.label if link else own_label,
            "is_linked_workspace": link is not None,
        }
        for wid, (name, link) in owners.items()
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
    ids = {t.workflow_id for g in groups for t in g.targets}
    names = await workflow_names(ids, organization_id)
    details = await target_details(ids, organization_id)
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
                    **details.get(t.workflow_id, {}),
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


def sample_note(eligible_calls: int, sticky_by_caller: bool) -> str:
    """How far actual percentages can be trusted for this many calls."""
    parts = []
    if eligible_calls < 100:
        parts.append(
            f"Only {eligible_calls} call(s) so far: with this few, actual "
            f"percentages can be far from the targets by chance. A 25% target "
            f"does not mean exactly 25 of every 100 calls."
        )
    else:
        parts.append(
            "Each call is a weighted draw, so actual percentages drift a few "
            "points around the targets even over hundreds of calls."
        )
    if sticky_by_caller:
        parts.append(
            "Sticky by caller is on: repeat callers return to the same agent, "
            "so callers who call often pull the actual split away from the "
            "targets."
        )
    return " ".join(parts)


def enrich_stats(
    stats: InboundRoutingStatsResponse,
    decisions: List[Dict[str, Any]],
    refusals: List[Dict[str, Any]],
    details: Dict[int, Dict[str, Any]],
    sticky_by_caller: bool,
) -> InboundRoutingStatsResponse:
    """Add picks, unavailability, fallbacks and refusals to ``stats``.

    ``decisions`` are the routing decisions stored on the runs
    (``list_routing_decisions_for_group``); ``refusals`` are the details of
    the group's ``call_refused`` audit rows in the same window."""
    rows = {r.workflow_id: r for r in stats.targets}

    def row_for(workflow_id: int) -> RoutingTargetStats:
        if workflow_id not in rows:
            rows[workflow_id] = RoutingTargetStats(
                workflow_id=workflow_id,
                calls=0,
                actual_percent=0.0,
                target_percent=0,
                is_target=False,
            )
        return rows[workflow_id]

    fallback_reasons: Dict[str, int] = {}
    fallback_calls = cross_workspace = 0
    for d in decisions:
        meta = d.get("meta") or {}
        picked = meta.get("picked_workflow_id")
        if isinstance(picked, int):
            row_for(picked).picked += 1
        for skipped in meta.get("skipped_targets") or []:
            wid = skipped.get("workflow_id")
            if not isinstance(wid, int):
                continue
            row = row_for(wid)
            row.unavailable += 1
            reason = str(skipped.get("reason") or "unknown")
            row.unavailable_reasons[reason] = row.unavailable_reasons.get(reason, 0) + 1
        took = d.get("workflow_id")
        fell_through = meta.get("source") == "fallback" or (
            isinstance(picked, int) and took != picked
        )
        if fell_through:
            fallback_calls += 1
            reason = meta.get("fallback_reason") or (
                "picked agent unavailable; next target took the call"
            )
            fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
            if isinstance(took, int):
                row_for(took).fallback_calls += 1
            if meta.get("changed_workspace"):
                cross_workspace += 1

    grouped: Dict[Tuple[str, Optional[int], Optional[int]], int] = {}
    for r in refusals:
        key = (
            str(r.get("reason") or "unknown"),
            r.get("organization_id"),
            r.get("workflow_id"),
        )
        grouped[key] = grouped.get(key, 0) + 1
        wid = r.get("workflow_id")
        if isinstance(wid, int):
            row = row_for(wid)
            row.unavailable += 1
            reason = key[0]
            row.unavailable_reasons[reason] = row.unavailable_reasons.get(reason, 0) + 1

    for wid, row in rows.items():
        info = details.get(wid) or {}
        row.agent_name = info.get("agent_name")
        row.organization_id = info.get("organization_id")
        row.organization_label = info.get("organization_label")

    stats.targets = [rows[w] for w in sorted(rows)]
    stats.fallback_calls = fallback_calls
    stats.fallback_reasons = fallback_reasons
    stats.cross_workspace_fallbacks = cross_workspace
    stats.refused_calls = len(refusals)
    stats.refusals = [
        RoutingRefusal(reason=reason, organization_id=org, workflow_id=wid, count=n)
        for (reason, org, wid), n in sorted(grouped.items(), key=lambda kv: -kv[1])
    ]
    stats.eligible_calls = stats.total_calls + len(refusals)
    stats.sticky_by_caller = sticky_by_caller
    stats.sample_note = sample_note(stats.eligible_calls, sticky_by_caller)
    return stats


async def group_stats(
    group: InboundRoutingGroupModel, organization_id: int, hours: int
) -> InboundRoutingStatsResponse:
    since = datetime.now(UTC) - timedelta(hours=hours)
    # Calls a split sent to a linked workspace's agent are that workspace's
    # runs, so read across the org and its linked workspaces.
    decisions = await db_client.list_routing_decisions_for_group(
        group.id,
        [organization_id, *await linked_organization_ids(organization_id)],
        since,
    )
    calls: Dict[int, int] = {}
    for d in decisions:
        calls[d["workflow_id"]] = calls.get(d["workflow_id"], 0) + 1
    refusal_rows = await db_client.list_inbound_routing_audit_since(
        organization_id, group.id, CALL_REFUSED_ACTION, since
    )
    refusals = [r.details or {} for r in refusal_rows]
    configured = {t.workflow_id: t.weight for t in group.targets}
    ids = set(configured) | set(calls)
    for d in decisions:
        meta = d.get("meta") or {}
        if isinstance(meta.get("picked_workflow_id"), int):
            ids.add(meta["picked_workflow_id"])
        ids.update(
            s["workflow_id"]
            for s in meta.get("skipped_targets") or []
            if isinstance(s.get("workflow_id"), int)
        )
    ids.update(
        r["workflow_id"] for r in refusals if isinstance(r.get("workflow_id"), int)
    )
    names = await workflow_names(ids, organization_id)
    stats = compute_stats(group.id, hours, since, configured, calls, names)
    return enrich_stats(
        stats,
        decisions,
        refusals,
        await target_details(ids, organization_id),
        group.sticky_by_caller,
    )


async def record_routed_call_refusal(
    routing_meta: Optional[Dict[str, Any]],
    number_organization_id: int,
    reason: str,
    called_number: Optional[str] = None,
) -> None:
    """Record a call a routing group routed that was refused before an agent
    answered. Stored in the number owner's audit log under the group, so the
    owner sees it in the group's stats and history. Holds no caller details.
    Never raises: the call is already being hung up."""
    if not routing_meta or not routing_meta.get("group_id"):
        return
    try:
        await db_client.add_inbound_routing_audit(
            organization_id=number_organization_id,
            group_id=routing_meta["group_id"],
            user_id=None,
            action=CALL_REFUSED_ACTION,
            details={
                "name": routing_meta.get("group_name"),
                "reason": reason,
                "workflow_id": routing_meta.get("workflow_id"),
                "organization_id": routing_meta.get("answered_organization_id")
                or routing_meta.get("organization_id")
                or number_organization_id,
                "called_number": called_number,
                "source": routing_meta.get("source"),
            },
        )
    except Exception as e:
        logger.warning(
            f"Could not record refused routed call for group "
            f"{routing_meta.get('group_id')}: {e}"
        )
