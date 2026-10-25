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
    name="hoppwhistle",
    display_name="Hoppwhistle",
    description="Insurance CRM, licensing and routing for AI callbacks and qualification",
    llm_hint=(
        "Hoppwhistle is a post-call reporting integration for workflows that Hoppwhistle "
        "starts (AI Callback Recovery, AI Lead Qualifier). It sends the finished call's "
        "outcome, transcript, recording link, transfer attempts and usage back to "
        "Hoppwhistle, signed. It does not participate in the conversation graph and should "
        "not be connected to other nodes. The conversation's own tools are the Hoppwhistle "
        "tools installed by POST /api/v1/integrations/hoppwhistle/install."
    ),
    docs_url="https://docs.dograh.com/integrations/hoppwhistle",
    category=NodeCategory.integration,
    icon="ShieldCheck",
    examples=[
        NodeExample(
            name="hoppwhistle_reporting",
            data={
                "name": "Hoppwhistle",
                "hoppwhistle_enabled": True,
                "hoppwhistle_base_url": "https://agents.example.com",
                "hoppwhistle_signing_secret": "whsec_xxxxxxxxxxxxxxxx",
            },
        )
    ],
    graph_constraints=GraphConstraints(
        min_incoming=0, max_incoming=0, min_outgoing=0, max_outgoing=0, max_instances=1
    ),
    property_order=(
        "name",
        "hoppwhistle_enabled",
        "hoppwhistle_base_url",
        "hoppwhistle_signing_secret",
        "hoppwhistle_cost_per_minute_cents",
    ),
    field_overrides={
        "name": {
            "spec_default": "Hoppwhistle",
            "description": "Short identifier for this Hoppwhistle configuration.",
        },
        "hoppwhistle_base_url": {"required": True},
        "hoppwhistle_signing_secret": {"required": True},
    },
)
class HoppwhistleNodeData(BaseNodeData):
    hoppwhistle_enabled: bool = spec_field(
        default=True,
        ui_type=PropertyType.boolean,
        display_name="Enabled",
        description="When false, finished calls are not reported to Hoppwhistle.",
    )
    hoppwhistle_base_url: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Hoppwhistle URL",
        description="Base URL of the Hoppwhistle API, e.g. https://agents.example.com.",
    )
    hoppwhistle_signing_secret: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Signing secret",
        description=(
            "The shared secret (Hoppwhistle's DOGRAH_AI_AGENT_SECRET). Reports are signed "
            "with HMAC-SHA256; the secret itself is never sent."
        ),
    )
    hoppwhistle_cost_per_minute_cents: float | None = spec_field(
        default=None,
        ui_type=PropertyType.number,
        display_name="Cost per minute (cents)",
        description=(
            "Optional: your all-in AI cost per minute, reported with each call so "
            "Hoppwhistle can show AI cost. Blank: Hoppwhistle estimates it."
        ),
    )

    @model_validator(mode="after")
    def _validate_enabled_config(self) -> "HoppwhistleNodeData":
        if not self.hoppwhistle_enabled:
            return self
        missing = [
            field
            for field in ("hoppwhistle_base_url", "hoppwhistle_signing_secret")
            if not (getattr(self, field) or "").strip()
        ]
        if missing:
            raise ValueError(
                f"Hoppwhistle node is enabled but missing required fields: {', '.join(missing)}"
            )
        if (
            not (self.hoppwhistle_base_url or "")
            .strip()
            .startswith(("http://", "https://"))
        ):
            raise ValueError("hoppwhistle_base_url must be an http(s) URL")
        return self


SPEC = build_spec(HoppwhistleNodeData)

NODE = IntegrationNodeRegistration(
    type_name="hoppwhistle",
    data_model=HoppwhistleNodeData,
    node_spec=SPEC,
    sensitive_fields=("hoppwhistle_signing_secret",),
)
