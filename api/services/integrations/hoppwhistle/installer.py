"""Install the Hoppwhistle tools (and, optionally, the starter workflows) into
an organization.

Idempotent: a tool that already exists in the organization by name and
category is updated in place, so re-running after Hoppwhistle moves (a new
base URL, a rotated secret) repoints every workflow that references the tools
without editing a single workflow. Everything is organization-scoped: an
existing credential is looked up with the caller's organization, never trusted
by UUID alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field, field_validator

from api.db import db_client
from api.enums import ToolStatus
from api.schemas.tool import CreateToolRequest

from .templates import WORKFLOW_NAMES, Product, build_workflow
from .tool_catalog import CONTRACT_VERSION, build_tool_definitions

_ALL_PRODUCTS: tuple[Product, ...] = ("CALLBACK_RECOVERY", "LEAD_QUALIFIER")


class HoppwhistleInstallRequest(BaseModel):
    base_url: str = Field(
        description="Hoppwhistle API base URL, e.g. https://agents.example.com."
    )
    service_secret: Optional[str] = Field(
        default=None,
        min_length=16,
        description=(
            "Hoppwhistle's DOGRAH_AI_AGENT_SECRET. Stored as the tools' bearer credential "
            "and as the reporting node's signing secret. Omit to reuse credential_uuid."
        ),
    )
    credential_uuid: Optional[str] = Field(
        default=None, description="An existing bearer credential in this organization."
    )
    create_workflows: bool = Field(
        default=False, description="Also create the starter workflows."
    )
    products: list[Literal["CALLBACK_RECOVERY", "LEAD_QUALIFIER"]] = Field(
        default_factory=lambda: list(_ALL_PRODUCTS)
    )
    include_after_hours: bool = Field(
        default=True,
        description="With CALLBACK_RECOVERY, also the after-hours inbound workflow.",
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return value


class InstallError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class InstallResult:
    contract_version: str
    credential_uuid: str
    tools: dict[str, str]
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)


async def _resolve_credential(
    request: HoppwhistleInstallRequest, organization_id: int, user_id: int
) -> str:
    if request.credential_uuid:
        credential = await db_client.get_credential_by_uuid(
            request.credential_uuid, organization_id
        )
        if not credential:
            raise InstallError(
                "Credential not found in this organization", status_code=404
            )
        if credential.credential_type != "bearer_token":
            raise InstallError("The credential must be a bearer token")
        return str(credential.credential_uuid)
    if not request.service_secret:
        raise InstallError("Give service_secret or credential_uuid")
    credential = await db_client.create_credential(
        organization_id=organization_id,
        user_id=user_id,
        name="Hoppwhistle service",
        credential_type="bearer_token",
        credential_data={"token": request.service_secret},
        description="Bearer credential for the Hoppwhistle AI agent tools.",
    )
    return str(credential.credential_uuid)


async def install_hoppwhistle(
    request: HoppwhistleInstallRequest, *, organization_id: int, user_id: int
) -> InstallResult:
    credential_uuid = await _resolve_credential(request, organization_id, user_id)
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
        # The same validation the tools API applies, so an install can never
        # store a definition the editor would refuse.
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

    if request.create_workflows:
        variants: list[tuple[Product, bool]] = []
        for requested in request.products:
            variants.append((requested, False))
            if requested == "CALLBACK_RECOVERY" and request.include_after_hours:
                variants.append((requested, True))
        for product, inbound in variants:
            definition = build_workflow(
                product,
                result.tools,
                base_url=request.base_url,
                signing_secret=request.service_secret,
                inbound=inbound,
                credential_uuid=credential_uuid,
            )
            workflow = await db_client.create_workflow(
                name=WORKFLOW_NAMES[(product, inbound)],
                workflow_definition=definition,
                user_id=user_id,
                organization_id=organization_id,
            )
            result.workflows.append(
                {
                    "product": product,
                    "inbound": inbound,
                    "workflow_id": workflow.id,
                    "workflow_uuid": getattr(workflow, "workflow_uuid", None),
                    "name": workflow.name,
                }
            )

    logger.info(
        f"Hoppwhistle installed for organization {organization_id}: "
        f"created={result.created} updated={result.updated} workflows={len(result.workflows)}"
    )
    return result
