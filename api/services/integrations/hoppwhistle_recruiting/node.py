from __future__ import annotations

from pydantic import model_validator

from api.services.integrations.base import IntegrationNodeRegistration
from api.services.workflow.node_data import BaseNodeData
from api.services.workflow.node_specs._base import (
    GraphConstraints,
    NodeCategory,
    NodeExample,
    PropertyType,
)
from api.services.workflow.node_specs.model_spec import (
    build_spec,
    node_spec,
    spec_field,
)


@node_spec(
    name="hoppwhistle_recruiting",
    display_name="Hoppwhistle Recruiting",
    description="Reports AI phone interviews to Hoppwhistle's AI Recruiting Studio",
    llm_hint=(
        "Hoppwhistle Recruiting is a post-call reporting node for AI phone interviews that "
        "Hoppwhistle compiles and starts. It sends the finished call's transcript, recording "
        "link, steps visited, extracted variables and usage back to Hoppwhistle, signed. It does "
        "not take part in the conversation and must not be connected to other nodes. The "
        "workflow's protected steps are checked by Hoppwhistle before every call; edit "
        "recruiting workflows from the Studio, not here."
    ),
    docs_url="https://docs.dograh.com/integrations/hoppwhistle-recruiting",
    category=NodeCategory.integration,
    icon="Briefcase",
    examples=[
        NodeExample(
            name="hoppwhistle_recruiting_reporting",
            data={
                "name": "Hoppwhistle Recruiting",
                "recruiting_enabled": True,
                "recruiting_base_url": "https://agents.example.com",
                "recruiting_signing_secret": "whsec_xxxxxxxxxxxxxxxx",
            },
        )
    ],
    graph_constraints=GraphConstraints(
        min_incoming=0, max_incoming=0, min_outgoing=0, max_outgoing=0, max_instances=1
    ),
    property_order=(
        "name",
        "recruiting_enabled",
        "recruiting_base_url",
        "recruiting_signing_secret",
        "recruiting_contract_version",
        "recruiting_framework_version",
    ),
    field_overrides={
        "name": {"spec_default": "Hoppwhistle Recruiting"},
        "recruiting_base_url": {"required": True},
        "recruiting_signing_secret": {"required": True},
    },
)
class HoppwhistleRecruitingNodeData(BaseNodeData):
    recruiting_enabled: bool = spec_field(
        default=True,
        ui_type=PropertyType.boolean,
        display_name="Enabled",
        description="When false, finished interviews are not reported (and Hoppwhistle will refuse to call).",
    )
    recruiting_base_url: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Hoppwhistle URL",
        description="Base URL of the Hoppwhistle API.",
    )
    recruiting_signing_secret: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Signing secret",
        description="Hoppwhistle's RECRUITING_VOICE_ENGINE_SECRET. Reports are HMAC-signed; the secret is never sent.",
    )
    recruiting_contract_version: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Contract version",
        description="Set by Hoppwhistle when it publishes the workflow.",
    )
    recruiting_protected_digest: str | None = spec_field(
        default=None, spec_exclude=True
    )
    recruiting_framework_version: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Interview framework version",
        description="Set by Hoppwhistle when it publishes the workflow.",
    )

    @model_validator(mode="after")
    def _validate_enabled_config(self) -> "HoppwhistleRecruitingNodeData":
        if not self.recruiting_enabled:
            return self
        missing = [
            f
            for f in ("recruiting_base_url", "recruiting_signing_secret")
            if not (getattr(self, f) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"Hoppwhistle Recruiting node is enabled but missing: {', '.join(missing)}"
            )
        if (
            not (self.recruiting_base_url or "")
            .strip()
            .startswith(("http://", "https://"))
        ):
            raise ValueError("recruiting_base_url must be an http(s) URL")
        return self


SPEC = build_spec(HoppwhistleRecruitingNodeData)

NODE = IntegrationNodeRegistration(
    type_name="hoppwhistle_recruiting",
    data_model=HoppwhistleRecruitingNodeData,
    node_spec=SPEC,
    sensitive_fields=("recruiting_signing_secret",),
)
