"""Shared phone numbers: one DID, routed by its owner to several workspaces.

A number is owned by exactly one workspace (its ``telephony_phone_numbers``
row and the routing group it is in). The owner can link other workspaces
(linked_workspaces.py, operator-set) and split the number's calls to their
agents. Nothing is copied: the linked workspace never gets a row for the
number, a carrier registration or the owner's carrier credentials.

"Shared with" is derived from the owner's routing, not stored separately, so
the two can't drift apart: a number is shared with a linked workspace while
its routing group has a target (any weight, 0 = paused) among that
workspace's agents. Both sides read the same rows:

* the owner sees every number it owns, its routing group and which linked
  workspaces it is shared with (:func:`owned_numbers`);
* a linked workspace sees, read-only, the numbers shared with it, who owns
  them, the group that controls them and its own share of the calls
  (:func:`numbers_shared_with`). It does not see the owner's other agents,
  callers, runs or carrier settings.

Only the owner changes routing; a linked workspace can't take the number over
either (:func:`shared_number_owner_for` guards its writes, and
:func:`choose_inbound_row` makes the owner's row win at call time).
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from loguru import logger

from api.db import db_client
from api.services.telephony.linked_workspaces import (
    linked_organization_ids,
    linked_workspaces,
    linking_workspaces,
    self_label,
)


@dataclass(frozen=True)
class SharedNumberOwner:
    organization_id: int
    label: str
    routing_group_id: Optional[int]


def _routed(row: Any) -> bool:
    return bool(
        getattr(row, "inbound_workflow_id", None)
        or getattr(row, "inbound_routing_group_id", None)
    )


def choose_inbound_row(
    rows: Sequence[Any], links: Mapping[int, Iterable[int]]
) -> Optional[Any]:
    """The row that answers a called number found in several workspaces.

    ``rows`` are the active rows for the number, one per workspace, in lookup
    order. ``links`` is ``{org_id: orgs it has linked}`` for those rows' orgs.

    A routed row (inbound agent or routing group) beats a caller-ID-only
    copy. Among several routed rows, a routing group beats a bare inbound
    agent, then the row of the workspace that linked the others (the owner:
    it decides where the shared number's calls go), then the lowest org id,
    so the answer never depends on which connection Asterisk picked.
    """
    if not rows:
        return None
    routed = [r for r in rows if _routed(r)]
    if not routed:
        return rows[0]
    if len(routed) == 1:
        return routed[0]

    orgs = {r.organization_id for r in routed}

    def rank(row: Any):
        linked = set(links.get(row.organization_id) or ())
        owns_others = bool(linked & (orgs - {row.organization_id}))
        return (
            0 if getattr(row, "inbound_routing_group_id", None) else 1,
            0 if owns_others else 1,
            row.organization_id,
        )

    chosen = min(routed, key=rank)
    logger.warning(
        f"Inbound number {getattr(chosen, 'address', '?')} is routed in "
        f"{len(routed)} workspaces ({sorted(orgs)}); answering for org "
        f"{chosen.organization_id}. Remove the inbound agent from the others."
    )
    return chosen


async def shared_number_owner_for(
    organization_id: int, address: str, provider: str
) -> Optional[SharedNumberOwner]:
    """The workspace that controls ``address`` for ``organization_id``, when
    ``organization_id`` is a linked workspace and a workspace that linked it
    routes this number. A linked workspace must not route such a number
    itself: its inbound calls belong to the owner's routing."""
    for owner in await linking_workspaces(organization_id):
        try:
            row = await db_client.find_active_phone_number_for_inbound(
                owner.organization_id, address, provider
            )
        except Exception as e:
            logger.warning(
                f"Could not check whether org {owner.organization_id} owns "
                f"{address}: {e}"
            )
            continue
        if row and _routed(row):
            return SharedNumberOwner(
                organization_id=owner.organization_id,
                label=owner.label,
                routing_group_id=getattr(row, "inbound_routing_group_id", None),
            )
    return None


def _percent(weight: int, total: int) -> float:
    return round(100.0 * weight / total, 1) if total else 0.0


async def owned_numbers(organization_id: int) -> Dict[str, Any]:
    """Numbers this workspace owns, with the routing that controls each and
    the linked workspaces it shares them with."""
    links = {
        w.organization_id: w.label for w in await linked_workspaces(organization_id)
    }
    rows = await db_client.list_numbers_with_routing(organization_id)
    groups = {
        g.id: g for g in await db_client.list_inbound_routing_groups(organization_id)
    }
    # Which linked workspace each target agent belongs to.
    target_ids = {t.workflow_id for g in groups.values() for t in g.targets}
    owner_of: Dict[int, int] = {}
    for org_id in links:
        for w in await db_client.get_workflows_by_ids(sorted(target_ids), org_id):
            owner_of[w.id] = org_id

    numbers: List[Dict[str, Any]] = []
    for r in rows:
        group = groups.get(r["inbound_routing_group_id"])
        shared: Dict[int, int] = {}
        total = 0
        if group:
            total = sum(t.weight for t in group.targets if t.weight > 0)
            for t in group.targets:
                org = owner_of.get(t.workflow_id)
                if org is not None:
                    shared[org] = shared.get(org, 0) + max(t.weight, 0)
        numbers.append(
            {
                **r,
                "routing_group_name": group.name if group else None,
                "routing_group_active": group.is_active if group else None,
                "shared_with": [
                    {
                        "organization_id": org,
                        "label": links[org],
                        "share_percent": _percent(weight, total),
                    }
                    for org, weight in sorted(shared.items())
                ],
            }
        )
    return {
        "organization_label": await self_label(organization_id) or "This workspace",
        "numbers": numbers,
    }


async def numbers_shared_with(organization_id: int) -> List[Dict[str, Any]]:
    """Numbers other workspaces own and route (partly) to this workspace's
    agents. Read-only: only the owner changes them."""
    result: List[Dict[str, Any]] = []
    for owner in await linking_workspaces(organization_id):
        # The owner must still list this workspace (an unlink is immediate).
        if organization_id not in await linked_organization_ids(owner.organization_id):
            continue
        groups = await db_client.list_inbound_routing_groups(owner.organization_id)
        if not groups:
            continue
        target_ids = sorted({t.workflow_id for g in groups for t in g.targets})
        mine = {
            w.id: w.name
            for w in await db_client.get_workflows_by_ids(target_ids, organization_id)
        }
        relevant = [g for g in groups if any(t.workflow_id in mine for t in g.targets)]
        if not relevant:
            continue
        rows = await db_client.list_numbers_with_routing(
            owner.organization_id, group_ids=[g.id for g in relevant]
        )
        by_group: Dict[int, List[Dict[str, Any]]] = {}
        for r in rows:
            by_group.setdefault(r["inbound_routing_group_id"], []).append(r)
        for g in relevant:
            total = sum(t.weight for t in g.targets if t.weight > 0)
            my_targets = [t for t in g.targets if t.workflow_id in mine]
            my_weight = sum(max(t.weight, 0) for t in my_targets)
            for r in by_group.get(g.id, []):
                result.append(
                    {
                        "address": r["address"],
                        "is_active": r["is_active"],
                        "owner_organization_id": owner.organization_id,
                        "owner_label": owner.label,
                        "routing_group_id": g.id,
                        "routing_group_name": g.name,
                        "routing_group_active": g.is_active,
                        "sticky_by_caller": g.sticky_by_caller,
                        "my_share_percent": _percent(my_weight, total),
                        "other_share_percent": (
                            round(100.0 - _percent(my_weight, total), 1)
                            if total
                            else 0.0
                        ),
                        "my_agents": [
                            {
                                "workflow_id": t.workflow_id,
                                "name": mine[t.workflow_id],
                                "share_percent": _percent(max(t.weight, 0), total),
                            }
                            for t in sorted(my_targets, key=lambda t: t.workflow_id)
                        ],
                    }
                )
    return sorted(result, key=lambda n: (n["owner_organization_id"], n["address"]))
