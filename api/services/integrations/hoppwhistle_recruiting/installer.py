"""Install (or refresh) the recruiting tools, and save Hoppwhistle-compiled workflows.

Everything is organization-scoped: tools, credentials and workflows are read
and written with the caller's organization id, never trusted by UUID alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel, Field, field_validator

from api.db import db_client
from api.enums import ToolStatus
from api.schemas.tool import CreateToolRequest

from .protected import protected_digest
from .tool_catalog import (
    CONTRACT_VERSION,
    TOOL_NAMES,
    build_tool_definitions,
    is_recruiting_tool,
)

CREDENTIAL_NAME = "Hoppwhistle recruiting service"
RECRUITING_NODE_TYPE = "hoppwhistle_recruiting"
#: Keys Hoppwhistle's compiler adds to node data that the engine does not store.
_COMPILER_KEYS = ("tool_names", "hr_module")


class InstallError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("base_url must be an http(s) URL")
    return value


class RecruitingInstallRequest(BaseModel):
    base_url: str = Field(description="Hoppwhistle API base URL.")
    service_secret: str = Field(
        min_length=16, description="Hoppwhistle's RECRUITING_VOICE_ENGINE_SECRET."
    )

    _v = field_validator("base_url")(classmethod(lambda cls, v: _validate_base_url(v)))


class RecruitingWorkflowRequest(BaseModel):
    workflow_uuid: Optional[str] = Field(
        default=None, description="Update this workflow; omit to create one."
    )
    name: str = Field(min_length=1, max_length=200)
    workflow_definition: dict[str, Any]
    base_url: str
    signing_secret: str = Field(min_length=16)
    publish: bool = True

    _v = field_validator("base_url")(classmethod(lambda cls, v: _validate_base_url(v)))


@dataclass
class InstallResult:
    contract_version: str
    credential_uuid: str
    tools: dict[str, str]
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)


async def _ensure_credential(organization_id: int, user_id: int, secret: str) -> str:
    """One bearer credential per organization for the recruiting tools, rotated in place."""
    existing = await db_client.get_credentials_for_organization(organization_id)
    for credential in existing or []:
        if (
            getattr(credential, "name", None) == CREDENTIAL_NAME
            and credential.credential_type == "bearer_token"
        ):
            await db_client.update_credential(
                str(credential.credential_uuid),
                organization_id,
                credential_data={"token": secret},
            )
            return str(credential.credential_uuid)
    created = await db_client.create_credential(
        organization_id=organization_id,
        user_id=user_id,
        name=CREDENTIAL_NAME,
        credential_type="bearer_token",
        credential_data={"token": secret},
        description="Bearer credential for the Hoppwhistle recruiting tools (RECRUITING_VOICE_ENGINE_SECRET).",
    )
    return str(created.credential_uuid)


async def install_recruiting_tools(
    request: RecruitingInstallRequest, *, organization_id: int, user_id: int
) -> InstallResult:
    credential_uuid = await _ensure_credential(
        organization_id, user_id, request.service_secret
    )
    existing = await db_client.get_tools_for_organization(organization_id)
    by_key = {
        (tool.name, tool.category): tool
        for tool in existing
        if tool.status != ToolStatus.ARCHIVED.value
    }
    result = InstallResult(
        contract_version=CONTRACT_VERSION, credential_uuid=credential_uuid, tools={}
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
    logger.info(
        f"Hoppwhistle recruiting tools installed for organization {organization_id}: "
        f"created={result.created} updated={result.updated}"
    )
    return result


async def recruiting_tool_uuids(organization_id: int) -> dict[str, str]:
    """name -> uuid for the installed recruiting tools in this organization."""
    tools = await db_client.get_tools_for_organization(organization_id)
    out: dict[str, str] = {}
    for tool in tools:
        if tool.status == ToolStatus.ARCHIVED.value:
            continue
        url = ((tool.definition or {}).get("config") or {}).get("url")
        if is_recruiting_tool(tool.name, tool.category, url):
            out[tool.name] = str(tool.tool_uuid)
    return out


def resolve_definition(
    definition: dict[str, Any],
    tools: dict[str, str],
    *,
    base_url: str,
    signing_secret: str,
) -> dict[str, Any]:
    """Replace `tool_names` with this organization's tool UUIDs and fill the reporting node.

    Raises InstallError when a node names a tool that is not installed, or a
    tool outside the recruiting set: a recruiting workflow never carries any
    other tool.
    """
    nodes_out: list[dict[str, Any]] = []
    recruiting_nodes = 0
    for node in definition.get("nodes") or []:
        data = dict(node.get("data") or {})
        names = data.get("tool_names")
        if names is not None:
            unknown = [n for n in names if n not in TOOL_NAMES]
            if unknown:
                raise InstallError(
                    f"Node {node.get('id')} names tools outside the recruiting set: {unknown}"
                )
            missing = [n for n in names if n not in tools]
            if missing:
                raise InstallError(
                    f"Recruiting tools are not installed in this organization: {missing}. Run install first.",
                    status_code=409,
                )
            data["tool_uuids"] = [tools[n] for n in names]
        for key in _COMPILER_KEYS:
            data.pop(key, None)
        if node.get("type") == RECRUITING_NODE_TYPE:
            recruiting_nodes += 1
            data["recruiting_base_url"] = base_url
            data["recruiting_signing_secret"] = signing_secret
            data["recruiting_enabled"] = True
        nodes_out.append({**node, "data": data})
    if recruiting_nodes != 1:
        raise InstallError(
            "A recruiting workflow must carry exactly one Hoppwhistle Recruiting node."
        )
    resolved = {**definition, "nodes": nodes_out}
    digest = protected_digest(resolved)
    for node in resolved["nodes"]:
        if node.get("type") == RECRUITING_NODE_TYPE:
            node["data"]["recruiting_protected_digest"] = digest
    return resolved


async def upsert_recruiting_workflow(
    request: RecruitingWorkflowRequest, *, organization_id: int, user_id: int
) -> dict[str, Any]:
    from api.services.workflow.dto import ReactFlowDTO
    from api.services.workflow.workflow_graph import WorkflowGraph

    tools = await recruiting_tool_uuids(organization_id)
    definition = resolve_definition(
        request.workflow_definition,
        tools,
        base_url=request.base_url,
        signing_secret=request.signing_secret,
    )
    # The same checks the editor's publish gate runs.
    try:
        WorkflowGraph(ReactFlowDTO.model_validate(definition))
    except Exception as exc:  # noqa: BLE001 - surfaced to Hoppwhistle verbatim
        raise InstallError(
            f"The compiled workflow is not valid for this engine: {exc}",
            status_code=422,
        ) from exc

    if request.workflow_uuid:
        workflow = await db_client.get_workflow_by_uuid(
            request.workflow_uuid, organization_id
        )
        if workflow is None:
            raise InstallError(
                "Workflow not found in this organization", status_code=404
            )
        await db_client.update_workflow(
            workflow_id=workflow.id,
            name=request.name,
            workflow_definition=definition,
            template_context_variables=None,
            workflow_configurations=None,
            organization_id=organization_id,
        )
    else:
        workflow = await db_client.create_workflow(
            name=request.name,
            workflow_definition=definition,
            user_id=user_id,
            organization_id=organization_id,
        )
        # A new workflow starts with its definition as a draft or as version 1,
        # depending on the engine version; make sure a draft holds exactly this.
        await db_client.save_workflow_draft(workflow.id, workflow_definition=definition)

    version_number = None
    if request.publish:
        draft = await db_client.get_draft_version(workflow.id)
        if draft is not None:
            published = await db_client.publish_workflow_draft(workflow.id)
            version_number = published.version_number
    refreshed = await db_client.get_workflow_by_uuid(
        str(workflow.workflow_uuid), organization_id
    )
    if (
        version_number is None
        and refreshed is not None
        and refreshed.released_definition is not None
    ):
        version_number = refreshed.released_definition.version_number
    return {
        "workflow_id": workflow.id,
        "workflow_uuid": str(workflow.workflow_uuid),
        "version_number": version_number,
        "contract_version": CONTRACT_VERSION,
    }
