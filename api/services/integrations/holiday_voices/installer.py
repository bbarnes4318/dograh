"""Install (or refresh) the Holiday Voices agents in ONE organization: the
store's own voice workspace.

Safe to re-run; nothing is ever duplicated:

* **Credential.** One bearer credential named ``CREDENTIAL_NAME`` holds the
  store's engine key. Re-running with a new key (after a rotation in
  Hoppwhistle) updates it in place; without a key, the existing one is reused.
* **Tools.** Matched by (name, category) inside the organization and updated
  in place, so a new URL or key repoints every workflow without editing one.
* **Workflows.** Matched by the ``holiday_voices`` reporting node's
  ``holiday_voices_agent`` (so a renamed workflow is still found), else by
  its starter name. An existing workflow is NEVER overwritten: it is left as
  the store customised it, unless ``refresh_workflows`` is set -- and even
  then the new starter is saved as a DRAFT the store must publish.

Isolation: the organization is the caller's (route) or the one named on the
command line (CLI); credentials are looked up with it, never by UUID alone.
An organization that already carries the insurance product's tools
(``/api/internal/ai-agent/`` URLs) is refused: Holiday Voices must never be
installed into the insurance workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel, Field, field_validator

from api.db import db_client
from api.enums import ToolStatus
from api.schemas.tool import CreateToolRequest

from .templates import (
    ALL_AGENTS,
    INBOUND,
    TEMPLATE_REVISION,
    WORKFLOW_NAMES,
    AgentKey,
    build_workflow,
)
from .tool_catalog import CONTRACT_VERSION, DISPOSITIONS, build_tool_definitions

CREDENTIAL_NAME = "Holiday Voices engine key"
#: The API key Hoppwhistle uses to place THIS organization's outbound calls
#: (POST /api/v1/public/agent/workflow/{uuid}). Scoped to this organization,
#: so it can only start the store's own workflows.
OUTBOUND_API_KEY_NAME = "Holiday Voices outbound (Hoppwhistle)"
INSURANCE_ROUTE_MARKER = "/api/internal/ai-agent/"


class HolidayVoicesInstallRequest(BaseModel):
    base_url: str = Field(
        description="Hoppwhistle API base URL, e.g. https://agents.example.com."
    )
    engine_key: Optional[str] = Field(
        default=None,
        description=(
            "The store's engine key (hvk_...). Stored once, as this organization's "
            "bearer credential. Omit to reuse the credential from an earlier install."
        ),
    )
    agents: list[AgentKey] = Field(default_factory=lambda: list(ALL_AGENTS))
    create_workflows: bool = Field(
        default=True, description="Create any starter agent workflow that is missing."
    )
    refresh_workflows: bool = Field(
        default=False,
        description=(
            "For agents that already exist, save the newest starter as a draft (never "
            "published automatically; the store's published version keeps running)."
        ),
    )
    cost_per_minute_cents: Optional[float] = Field(default=None, ge=0, le=1000)

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("engine_key")
    @classmethod
    def _engine_key(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value.startswith("hvk_") or len(value) < 24:
            raise ValueError("engine_key must be a Holiday Voices engine key (hvk_...)")
        return value


class InstallError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class InstallResult:
    contract_version: str
    template_revision: int
    organization_id: int
    credential_uuid: str
    credential: str  # created | updated | reused
    tools: dict[str, str]
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)


async def _resolve_credential(
    request: HolidayVoicesInstallRequest, organization_id: int, user_id: int
) -> tuple[str, str]:
    existing = [
        c
        for c in await db_client.get_credentials_for_organization(organization_id)
        if c.name == CREDENTIAL_NAME and c.credential_type == "bearer_token"
    ]
    current = existing[0] if existing else None
    if request.engine_key:
        if current:
            await db_client.update_credential(
                str(current.credential_uuid),
                organization_id,
                credential_data={"token": request.engine_key},
            )
            return str(current.credential_uuid), "updated"
        created = await db_client.create_credential(
            organization_id=organization_id,
            user_id=user_id,
            name=CREDENTIAL_NAME,
            credential_type="bearer_token",
            credential_data={"token": request.engine_key},
            description="The Holiday Voices store's engine key for its commerce tools.",
        )
        return str(created.credential_uuid), "created"
    if current:
        return str(current.credential_uuid), "reused"
    raise InstallError(
        "No engine key on file for this organization: pass engine_key "
        "(hv-workspace engine-key in Hoppwhistle issues one)."
    )


def _hv_agent_of(definition: Any) -> Optional[str]:
    nodes = definition.get("nodes") if isinstance(definition, dict) else None
    for node in nodes or []:
        if isinstance(node, dict) and node.get("type") == "holiday_voices":
            agent = (node.get("data") or {}).get("holiday_voices_agent")
            if isinstance(agent, str):
                return agent
    return None


def _definition_of(workflow: Any) -> Any:
    current = getattr(workflow, "current_definition", None)
    if current is not None and getattr(current, "workflow_json", None):
        return current.workflow_json
    return getattr(workflow, "workflow_definition", None)


async def _assert_not_insurance(organization_id: int) -> list[Any]:
    tools = await db_client.get_tools_for_organization(organization_id)
    for tool in tools:
        if tool.status == ToolStatus.ARCHIVED.value:
            continue
        config = (tool.definition or {}).get("config") or {}
        urls = [config.get("url"), (config.get("resolver") or {}).get("url")]
        if any(isinstance(u, str) and INSURANCE_ROUTE_MARKER in u for u in urls):
            raise InstallError(
                "This organization runs the insurance AI agents. Holiday Voices must be "
                "installed in its own voice workspace (scripts/holiday-voices-setup.sh).",
                status_code=409,
            )
    return tools


async def install_holiday_voices(
    request: HolidayVoicesInstallRequest, *, organization_id: int, user_id: int
) -> InstallResult:
    existing_tools = await _assert_not_insurance(organization_id)
    credential_uuid, credential_state = await _resolve_credential(
        request, organization_id, user_id
    )
    by_key = {
        (tool.name, tool.category): tool
        for tool in existing_tools
        if tool.status != ToolStatus.ARCHIVED.value
    }
    result = InstallResult(
        contract_version=CONTRACT_VERSION,
        template_revision=TEMPLATE_REVISION,
        organization_id=organization_id,
        credential_uuid=credential_uuid,
        credential=credential_state,
        tools={},
    )

    for spec in build_tool_definitions(request.base_url, credential_uuid):
        validated = CreateToolRequest.model_validate(spec)
        definition = validated.model_dump(mode="json")["definition"]
        current = by_key.get((spec["name"], spec["category"]))
        if current:
            await db_client.update_tool(
                str(current.tool_uuid),
                organization_id,
                description=spec["description"],
                definition=definition,
            )
            result.tools[spec["name"]] = str(current.tool_uuid)
            result.updated.append(spec["name"])
        else:
            tool = await db_client.create_tool(
                organization_id=organization_id,
                user_id=user_id,
                name=spec["name"],
                definition=definition,
                category=spec["category"],
                description=spec["description"],
                icon=spec["icon"],
                icon_color=spec["icon_color"],
            )
            result.tools[spec["name"]] = str(tool.tool_uuid)
            result.created.append(spec["name"])

    workflows = await db_client.get_all_workflows(organization_id=organization_id)
    for agent in request.agents:
        definition = build_workflow(
            agent,
            result.tools,
            base_url=request.base_url,
            credential_uuid=credential_uuid,
        )
        if request.cost_per_minute_cents is not None:
            for node in definition["nodes"]:
                if node["type"] == "holiday_voices":
                    node["data"]["holiday_voices_cost_per_minute_cents"] = (
                        request.cost_per_minute_cents
                    )
        match = next(
            (w for w in workflows if _hv_agent_of(_definition_of(w)) == agent),
            None,
        ) or next((w for w in workflows if w.name == WORKFLOW_NAMES[agent]), None)

        entry: dict[str, Any] = {"agent": agent, "inbound": INBOUND[agent]}
        target = match
        if match is not None:
            if request.refresh_workflows:
                await db_client.save_workflow_draft(
                    match.id, workflow_definition=definition
                )
                entry["action"] = "draft_saved"
            else:
                entry["action"] = "kept"
        elif request.create_workflows:
            target = await db_client.create_workflow(
                name=WORKFLOW_NAMES[agent],
                workflow_definition=definition,
                user_id=user_id,
                organization_id=organization_id,
            )
            for code in DISPOSITIONS:
                await db_client.add_call_disposition_code(target.id, code)
            entry["action"] = "created"
        else:
            entry["action"] = "missing"
        if target is not None:
            workflow_uuid = getattr(target, "workflow_uuid", None)
            entry.update(
                workflow_id=target.id,
                name=target.name,
                workflow_uuid=str(workflow_uuid) if workflow_uuid else None,
            )
        result.workflows.append(entry)

    logger.info(
        f"Holiday Voices installed for organization {organization_id}: "
        f"credential={credential_state} created={result.created} "
        f"updated={len(result.updated)} workflows="
        f"{[(w['agent'], w['action']) for w in result.workflows]}"
    )
    return result


@dataclass
class OutboundKeyResult:
    action: str  # created | kept | rotated
    key_prefix: str
    #: The raw key, only when it was created now. Shown once; never stored here.
    api_key: Optional[str] = None


async def issue_outbound_api_key(
    *, organization_id: int, user_id: int, rotate: bool = False
) -> OutboundKeyResult:
    """The organization's API key for Hoppwhistle's outbound calls.

    Idempotent: an active key with this name is kept (its raw value cannot be
    shown again). With ``rotate`` the old one is archived and a new one issued.
    Refuses the insurance workspace like the install does.
    """
    await _assert_not_insurance(organization_id)
    existing = [
        k
        for k in await db_client.get_api_keys_by_organization(organization_id)
        if k.name == OUTBOUND_API_KEY_NAME and k.is_active
    ]
    if existing and not rotate:
        return OutboundKeyResult(action="kept", key_prefix=existing[0].key_prefix)
    for key in existing:
        await db_client.archive_api_key(key.id)
    created, raw = await db_client.create_api_key(
        organization_id, OUTBOUND_API_KEY_NAME, created_by=user_id
    )
    return OutboundKeyResult(
        action="rotated" if existing else "created",
        key_prefix=created.key_prefix,
        api_key=raw,
    )
