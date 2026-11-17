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
    name="holiday_voices",
    display_name="Holiday Voices",
    description="Report finished calls to the Holiday Voices store in Hoppwhistle",
    llm_hint=(
        "Holiday Voices is a post-call reporting integration for the Holiday Voices "
        "retail store's agents (shopping, support, outbound sales, wholesale). After a "
        "call whose initial context carries hv_session_id/hv_session_token it posts the "
        "outcome, extracted facts, transfer attempts, recording/transcript links, latency "
        "and cost to Hoppwhistle with the store's engine-key credential. It is not part "
        "of the conversation graph and must not be connected to other nodes. The "
        "conversation's tools are installed by POST "
        "/api/v1/integrations/holiday-voices/install."
    ),
    docs_url="https://docs.dograh.com/integrations/holiday-voices",
    category=NodeCategory.integration,
    icon="Gift",
    examples=[
        NodeExample(
            name="holiday_voices_reporting",
            data={
                "name": "Holiday Voices",
                "holiday_voices_enabled": True,
                "holiday_voices_base_url": "https://agents.example.com",
                "holiday_voices_credential_uuid": "00000000-0000-0000-0000-000000000000",
                "holiday_voices_agent": "SHOPPING",
            },
        )
    ],
    graph_constraints=GraphConstraints(
        min_incoming=0, max_incoming=0, min_outgoing=0, max_outgoing=0, max_instances=1
    ),
    property_order=(
        "name",
        "holiday_voices_enabled",
        "holiday_voices_base_url",
        "holiday_voices_credential_uuid",
        "holiday_voices_agent",
        "holiday_voices_cost_per_minute_cents",
    ),
    field_overrides={
        "name": {
            "spec_default": "Holiday Voices",
            "description": "Short identifier for this Holiday Voices configuration.",
        },
        "holiday_voices_base_url": {"required": True},
        "holiday_voices_credential_uuid": {"required": True},
    },
)
class HolidayVoicesNodeData(BaseNodeData):
    holiday_voices_enabled: bool = spec_field(
        default=True,
        ui_type=PropertyType.boolean,
        display_name="Enabled",
        description="When false, finished calls are not reported to Holiday Voices.",
    )
    holiday_voices_base_url: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Hoppwhistle URL",
        description="Base URL of the Hoppwhistle API, e.g. https://agents.example.com.",
    )
    holiday_voices_credential_uuid: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Engine key credential",
        description=(
            "UUID of this organization's bearer credential holding the store's engine key "
            "(hvk_...). The same credential the Holiday Voices tools use; the key itself "
            "is never stored on the workflow."
        ),
    )
    holiday_voices_agent: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Agent",
        description="Which Holiday Voices agent this is: SHOPPING, SUPPORT, OUTBOUND_SALES or WHOLESALE.",
    )
    holiday_voices_cost_per_minute_cents: float | None = spec_field(
        default=None,
        ui_type=PropertyType.number,
        display_name="Cost per minute (cents)",
        description=(
            "Optional all-in AI cost per minute, so the store sees each call's cost. "
            "Blank: cost is reported as not known."
        ),
    )
    holiday_voices_template_revision: int | None = spec_field(
        default=None,
        ui_type=PropertyType.number,
        display_name="Starter revision",
        description="The starter template revision this workflow was installed from.",
        spec_exclude=True,
    )

    @model_validator(mode="after")
    def _validate_enabled_config(self) -> "HolidayVoicesNodeData":
        if not self.holiday_voices_enabled:
            return self
        missing = [
            field
            for field in ("holiday_voices_base_url", "holiday_voices_credential_uuid")
            if not (getattr(self, field) or "").strip()
        ]
        if missing:
            raise ValueError(
                "Holiday Voices node is enabled but missing required fields: "
                + ", ".join(missing)
            )
        if (
            not (self.holiday_voices_base_url or "")
            .strip()
            .startswith(("http://", "https://"))
        ):
            raise ValueError("holiday_voices_base_url must be an http(s) URL")
        return self


SPEC = build_spec(HolidayVoicesNodeData)

NODE = IntegrationNodeRegistration(
    type_name="holiday_voices",
    data_model=HolidayVoicesNodeData,
    node_spec=SPEC,
)
