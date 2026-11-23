"""Linked workspaces: another workspace's AI agents as inbound routing targets.

A workspace (organization) that owns phone numbers can link other workspaces.
Its routing groups may then split calls to those workspaces' agents too, for
example 50% to its own Final Expense agent and 50% to a Real Estate agent in
the real estate workspace. Such a call's run is created in the agent's
workspace, so it shows up there; the number, the routing group and its
settings stay with the workspace that owns the number.

The link is one-way and is set only by an operator, in the source
workspace's ``organization_configurations`` row
``INBOUND_ROUTING_LINKED_WORKSPACES`` (``scripts/link_workspaces.sql``). No
API writes it: linking hands another workspace calls to your numbers, which is
a decision about both tenants, not something either one's users can grant
themselves. Value::

    {"self_label": "Final Expense",
     "workspaces": [{"organization_id": 3, "label": "Real estate"}]}

``self_label`` (optional) is how the source workspace is named next to the
linked ones: on its own agents in the routing editor ("Alex — Final
Expense") and, in a linked workspace, as the owner of the numbers shared
with it. Without it the source is "This workspace" to itself and
"Workspace <id>" to the workspaces it links.

On ARI a linked workspace can only take a call when it has an ARI
configuration on the same Asterisk app as the number (the run needs one of
its own configurations for hang-ups and transfers). The provider-webhook path
(``/inbound/run``) never routes across workspaces; a split that picks a
linked agent there falls through to the next target.

If the setting can't be read, no workspace is linked: the call stays in the
number's own workspace, never dropped.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from loguru import logger

from api.db import db_client
from api.enums import OrganizationConfigurationKey

LINKED_WORKSPACES_KEY = (
    OrganizationConfigurationKey.INBOUND_ROUTING_LINKED_WORKSPACES.value
)


@dataclass(frozen=True)
class LinkedWorkspace:
    organization_id: int
    label: str


@dataclass(frozen=True)
class LinkingWorkspace:
    """A workspace that has linked this one: it owns numbers whose routing
    may send calls here. ``label`` is its ``self_label``; ``my_label`` is
    what it calls this workspace."""

    organization_id: int
    label: str
    my_label: str


def parse_self_label(value: Any, organization_id: int) -> str:
    """The source workspace's own name from its stored setting."""
    raw = value.get("self_label") if isinstance(value, dict) else None
    label = str(raw or "").strip()
    return label or f"Workspace {organization_id}"


def parse_linked_workspaces(value: Any, organization_id: int) -> List[LinkedWorkspace]:
    """The links in a stored setting value; malformed entries are ignored."""
    entries = value.get("workspaces") if isinstance(value, dict) else None
    links: dict[int, LinkedWorkspace] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("organization_id")
        try:
            org_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if isinstance(raw_id, bool) or org_id <= 0 or org_id == organization_id:
            continue
        label = str(entry.get("label") or "").strip() or f"Workspace {org_id}"
        links.setdefault(org_id, LinkedWorkspace(org_id, label))
    return sorted(links.values(), key=lambda link: link.organization_id)


async def linked_workspaces(organization_id: Optional[int]) -> List[LinkedWorkspace]:
    """The workspaces ``organization_id``'s routing groups may send calls to."""
    if not organization_id:
        return []
    try:
        row = await db_client.get_configuration(organization_id, LINKED_WORKSPACES_KEY)
    except Exception as e:
        logger.warning(
            f"Could not read linked workspaces for org {organization_id}; "
            f"routing within the org only: {e}"
        )
        return []
    return parse_linked_workspaces(row.value if row else None, organization_id)


async def linked_organization_ids(organization_id: Optional[int]) -> List[int]:
    return [link.organization_id for link in await linked_workspaces(organization_id)]


async def self_label(organization_id: Optional[int]) -> Optional[str]:
    """The workspace's operator-set ``self_label``, or None when unset or
    unreadable (callers then show their own default)."""
    if not organization_id:
        return None
    try:
        row = await db_client.get_configuration(organization_id, LINKED_WORKSPACES_KEY)
    except Exception as e:
        logger.warning(
            f"Could not read the routing label of org {organization_id}: {e}"
        )
        return None
    raw = row.value.get("self_label") if row and isinstance(row.value, dict) else None
    label = str(raw or "").strip()
    return label or None


async def linking_workspaces(organization_id: Optional[int]) -> List[LinkingWorkspace]:
    """The workspaces that have linked ``organization_id`` (the reverse of
    :func:`linked_workspaces`). Only an operator writes these rows, so this
    is the authoritative list of who may hand this workspace calls.

    If the settings can't be read, nobody has linked it: callers then show
    and allow nothing shared."""
    if not organization_id:
        return []
    try:
        rows = await db_client.get_configurations_by_key(LINKED_WORKSPACES_KEY)
    except Exception as e:
        logger.warning(
            f"Could not read which workspaces link org {organization_id}: {e}"
        )
        return []
    result: List[LinkingWorkspace] = []
    for row in rows:
        if row.organization_id == organization_id:
            continue
        for link in parse_linked_workspaces(row.value, row.organization_id):
            if link.organization_id == organization_id:
                result.append(
                    LinkingWorkspace(
                        organization_id=row.organization_id,
                        label=parse_self_label(row.value, row.organization_id),
                        my_label=link.label,
                    )
                )
    return sorted(result, key=lambda w: w.organization_id)
