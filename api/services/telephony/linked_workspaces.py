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

    {"workspaces": [{"organization_id": 3, "label": "Real estate"}]}

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
