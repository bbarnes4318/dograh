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
  target is tried, then the number's ``inbound_workflow_id``.
* Numbers with no group (or an inactive/empty one) use
  ``inbound_workflow_id`` exactly as before.

Group rows are read from the DB on every call (no cache), so a change made in
the UI applies to the next call. A routing config problem never drops a call:
any failure falls back to ``inbound_workflow_id``.
"""

import hashlib
import random
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


async def _usable_workflow(workflow_id: int, organization_id: int) -> Optional[str]:
    """None when the workflow can take the call, else why it can't."""
    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    if not workflow:
        return "not found in this organization"
    if workflow.status != WorkflowStatus.ACTIVE.value:
        return f"status is {workflow.status}"
    return None


async def resolve_inbound_workflow_id(
    phone_row, caller_number: Optional[str]
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Return ``(workflow_id, routing_meta)`` for an inbound call.

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
        for workflow_id in ordered:
            problem = await _usable_workflow(workflow_id, organization_id)
            if problem is None:
                meta["workflow_id"] = workflow_id
                meta["source"] = "group"
                if skipped:
                    meta["skipped_targets"] = skipped
                return workflow_id, meta
            logger.warning(
                f"Inbound routing: group {group_id} ({group.name}) target workflow "
                f"{workflow_id} can't take calls ({problem}); trying the next target"
            )
            skipped.append({"workflow_id": workflow_id, "reason": problem})

        meta["skipped_targets"] = skipped
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
