"""Pick the workflow that answers an inbound call.

Every inbound path (ARI and the provider ``/inbound/run`` webhook) calls
:func:`resolve_inbound_workflow_id` instead of reading
``phone_row.inbound_workflow_id`` directly.

Rules:

* If the called number belongs to an active routing group with at least one
  target of weight > 0, a target is picked by weight. With
  ``sticky_by_caller`` the pick is a stable hash of the caller's number, so the
  same person calling back reaches the same workflow while the weights stay the
  same; otherwise it is weighted random.
* If the picked workflow is missing, archived or in another org, the next
  target is tried, then the number's ``inbound_workflow_id``. A workflow in a
  linked workspace (linked_workspaces.py) counts as usable when the caller
  passes that workspace in ``linked_organization_ids``; the run is then
  created in that workspace (``routing_meta["organization_id"]``).
* Numbers with no group (or an inactive/empty one) use
  ``inbound_workflow_id`` exactly as before.

Fallback policy (explicit and recorded, never silent): when the weighted pick
can't take the call, the other targets with weight > 0 are tried in a fixed
order (workflow id), then the number's ``inbound_workflow_id``. Every skipped
target is recorded with its workspace and the reason
(``skipped_targets``), and ``changed_workspace`` is set when the call ends up
in a different workspace (business) than the one the weights picked. A
linked workspace this call path can't hand calls to (on ARI: no ARI
configuration on this Asterisk app) is passed in ``unavailable_organizations``
so the reason names it instead of "not found".

Group rows are read from the DB on every call (no cache), so a change made in
the UI applies to the next call. A routing config problem never drops a call:
any failure falls back to ``inbound_workflow_id``.
"""

import hashlib
import random
from datetime import UTC, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from api.db import db_client
from api.enums import WorkflowStatus
from api.utils.telephony_address import normalize_telephony_address

# Key under ``workflow_runs.initial_context`` that records the decision.
ROUTING_META_KEY = "inbound_routing"

_UNKNOWN_CALLERS = {"", "unknown", "anonymous", "restricted", "private"}


def normalize_caller(caller_number: Optional[str]) -> Optional[str]:
    """Canonical caller address for hashing, or None when there is no usable
    caller number (blocked/unknown caller ID)."""
    raw = (caller_number or "").strip()
    if raw.lower() in _UNKNOWN_CALLERS:
        return None
    # A bare 10-digit North American number is the same caller as its +1
    # form (carriers and PBXes send either). Without this the two hash to
    # different buckets and a sticky caller can land on another agent.
    # +E.164 callers keep the key they always had.
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not raw.startswith("+") and len(digits) == 10 and digits[0] in "23456789":
        return f"+1{digits}"
    try:
        return normalize_telephony_address(raw).canonical
    except Exception:
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits or raw.lower()


def caller_bucket(normalized_caller: str, modulus: int = 100) -> int:
    """Stable bucket in ``[0, modulus)`` from sha256 of the caller number."""
    digest = hashlib.sha256(normalized_caller.encode("utf-8")).hexdigest()
    return int(digest, 16) % modulus


def order_targets(
    weights: Sequence[Tuple[int, int]],
    caller_number: Optional[str],
    sticky_by_caller: bool,
    rng: Optional[random.Random] = None,
) -> Tuple[List[int], Dict[str, Any]]:
    """Order workflow ids for one call: the weighted pick first, then the
    other positive-weight targets as fallbacks.

    ``weights`` is ``[(workflow_id, weight), ...]``. Targets are mapped onto
    cumulative ranges in workflow-id order, so the mapping depends only on the
    weights, not on the order they were saved in.
    """
    positive = sorted((wid, w) for wid, w in weights if w and w > 0)
    if not positive:
        return [], {}
    total = sum(w for _, w in positive)

    normalized = normalize_caller(caller_number) if sticky_by_caller else None
    if normalized is not None:
        mode = "sticky"
        point = caller_bucket(normalized, total)
    else:
        mode = "random" if not sticky_by_caller else "random_no_caller_id"
        point = (rng or random).randrange(total)

    cumulative = 0
    chosen_index = len(positive) - 1
    for index, (_, weight) in enumerate(positive):
        cumulative += weight
        if point < cumulative:
            chosen_index = index
            break

    ordered = [positive[chosen_index][0]] + [
        wid for i, (wid, _) in enumerate(positive) if i != chosen_index
    ]
    return ordered, {"mode": mode, "bucket": point, "bucket_total": total}


async def _usable_workflow(
    workflow_id: int,
    organization_id: int,
    linked_organization_ids: Sequence[int] = (),
    unavailable_organizations: Mapping[int, str] = {},
) -> Tuple[Optional[str], int]:
    """``(None, org_id)`` when the workflow can take the call, with the org it
    belongs to (the number's org or a linked workspace), else
    ``(why not, org_id)`` with the org it was found in, when known."""
    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    owner = organization_id
    if not workflow:
        for linked_org in linked_organization_ids:
            if linked_org == organization_id:
                continue
            workflow = await db_client.get_workflow(
                workflow_id, organization_id=linked_org
            )
            if workflow:
                owner = linked_org
                break
    if not workflow:
        for other_org, why in unavailable_organizations.items():
            if other_org == organization_id or other_org in linked_organization_ids:
                continue
            if await db_client.get_workflow(workflow_id, organization_id=other_org):
                return (f"linked workspace {other_org} {why}", other_org)
    if not workflow:
        # Only to record which business the skipped agent belongs to (e.g. a
        # workspace that was unlinked); it is never routed to.
        try:
            other = await db_client.get_workflow_by_id(workflow_id)
        except Exception:
            other = None
        other_org = getattr(other, "organization_id", None)
        if isinstance(other_org, int) and other_org != organization_id:
            return (
                f"in workspace {other_org}, which is not linked to this "
                f"number's workspace",
                other_org,
            )
    if not workflow:
        return (
            "not found in this organization"
            + (" or its linked workspaces" if linked_organization_ids else ""),
            organization_id,
        )
    if workflow.status != WorkflowStatus.ACTIVE.value:
        return f"status is {workflow.status}", owner
    return None, owner


async def resolve_inbound_workflow_id(
    phone_row,
    caller_number: Optional[str],
    linked_organization_ids: Sequence[int] = (),
    unavailable_organizations: Optional[Mapping[int, str]] = None,
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Return ``(workflow_id, routing_meta)`` for an inbound call.

    ``linked_organization_ids`` are the linked workspaces (linked_workspaces.py)
    this call path can hand the call to. A target in one of them is usable;
    ``routing_meta["organization_id"]`` then names that workspace, and the run
    must be created there. Targets in any other org are skipped.
    ``unavailable_organizations`` (``{org_id: why}``) are linked workspaces
    this path can't hand calls to; their targets are skipped with that reason.

    ``routing_meta`` is None for numbers outside any group (today's
    behaviour, nothing extra is recorded); otherwise it explains the decision
    and is stored on the run's ``initial_context["inbound_routing"]``.
    ``workflow_id`` may be None only when the number has neither a usable
    group nor an ``inbound_workflow_id`` — the caller handles that as before.
    """
    fallback_id = phone_row.inbound_workflow_id
    group_id = getattr(phone_row, "inbound_routing_group_id", None)
    if not group_id:
        return fallback_id, None

    organization_id = phone_row.organization_id
    meta: Dict[str, Any] = {
        "group_id": group_id,
        "group_name": None,
        "decided_at": datetime.now(UTC).isoformat(),
        "fallback_workflow_id": fallback_id,
    }

    def fall_back(reason: str) -> Tuple[Optional[int], Dict[str, Any]]:
        meta["workflow_id"] = fallback_id
        meta["source"] = "fallback"
        meta["fallback_reason"] = reason
        return fallback_id, meta

    try:
        group = await db_client.get_inbound_routing_group_with_targets(group_id)
        if not group or group.organization_id != organization_id:
            logger.warning(
                f"Inbound routing: number {phone_row.id} points at routing group "
                f"{group_id}, which is missing or in another org; using "
                f"inbound_workflow_id {fallback_id}"
            )
            return fall_back("group_not_found")

        meta["group_name"] = group.name
        weights = [(t.workflow_id, t.weight) for t in group.targets]
        meta["weights"] = {str(wid): w for wid, w in sorted(weights)}
        meta["sticky_by_caller"] = group.sticky_by_caller

        if not group.is_active:
            return fall_back("group_inactive")

        ordered, pick = order_targets(weights, caller_number, group.sticky_by_caller)
        if not ordered:
            return fall_back("group_has_no_weighted_targets")
        meta.update(pick)
        meta["picked_workflow_id"] = ordered[0]

        skipped = []
        picked_org: Optional[int] = None
        for workflow_id in ordered:
            problem, owner = await _usable_workflow(
                workflow_id,
                organization_id,
                linked_organization_ids,
                unavailable_organizations or {},
            )
            if picked_org is None:
                picked_org = owner
                meta["picked_organization_id"] = owner
            if problem is None:
                meta["workflow_id"] = workflow_id
                meta["source"] = "group"
                meta["answered_organization_id"] = owner
                if owner != organization_id:
                    meta["organization_id"] = owner
                    meta["number_organization_id"] = organization_id
                if skipped:
                    meta["skipped_targets"] = skipped
                    meta["changed_workspace"] = owner != picked_org
                return workflow_id, meta
            logger.warning(
                f"Inbound routing: group {group_id} ({group.name}) target workflow "
                f"{workflow_id} can't take calls ({problem}); trying the next target"
            )
            skipped.append(
                {
                    "workflow_id": workflow_id,
                    "organization_id": owner,
                    "reason": problem,
                }
            )

        meta["skipped_targets"] = skipped
        meta["answered_organization_id"] = organization_id
        meta["changed_workspace"] = picked_org not in (None, organization_id)
        logger.warning(
            f"Inbound routing: no usable target in group {group_id} ({group.name}); "
            f"using inbound_workflow_id {fallback_id}"
        )
        return fall_back("no_usable_target")
    except Exception as e:
        logger.warning(
            f"Inbound routing: failed to resolve group {group_id} for number "
            f"{phone_row.id}: {e}; using inbound_workflow_id {fallback_id}"
        )
        return fall_back("resolver_error")
