"""Every finished call to Hoppwhistle's lead-intel endpoint (dograh-run-v1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from api.services.integrations.hoppwhistle.client import deliver_report, sign
from api.services.integrations.hoppwhistle.lead_intel import (
    LEAD_INTEL_PATH,
    build_lead_intel_payload,
    lead_intel_config,
)


def _run(**over):
    fields = {
        "id": 981,
        "workflow_id": 42,
        "workflow": SimpleNamespace(name="Robert inbound"),
        "campaign_id": None,
        "call_type": "inbound",
        "created_at": datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
        "initial_context": {
            "direction": "inbound",
            "caller_number": "+18655551234",
            "called_number": "+18005550100",
            "inbound_routing": {"did": "+18005550100", "nested": {"x": 1}},
        },
        "gathered_context": {
            "call_disposition": "transferred",
            "call_tags": ["seller"],
            "node_path": ["start", "qualify", "transfer"],
            "extracted_variables": {
                "first_name": "Pat",
                "property_address": "12 Oak Street",
                "asking_price": 185000,
                "owns_property": True,
                "notes": None,
            },
            "transfer_attempts": [{"status": "success", "destination": "+18655550000"}],
            "nodes_visited": ["start"],
            "_secret": "x",
        },
        "usage_info": {"call_duration_seconds": 95.5},
        "transcript_text": "assistant: Hi, this is Robert. | user: Hi.",
        "recording_url": "recordings/981.wav",
        "transcript_url": "transcripts/981.txt",
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def test_it_is_off_unless_both_settings_are_present():
    assert lead_intel_config({}) is None
    assert lead_intel_config({"HOPPWHISTLE_LEAD_INTEL_URL": "https://x"}) is None
    assert lead_intel_config(
        {
            "HOPPWHISTLE_LEAD_INTEL_URL": "https://x/",
            "DOGRAH_QUALITY_INGEST_SECRET": "s",
        }
    ) == ("https://x", "s")


def test_an_inbound_call_carries_the_caller_and_what_the_agent_captured():
    body = build_lead_intel_payload(_run(), public_token="tok")
    assert body["externalRunId"] == "981"
    assert body["workflowId"] == "42"
    assert body["workflowName"] == "Robert inbound"
    assert body["callType"] == "inbound"
    assert body["customerNumber"] == "+18655551234"
    assert body["ourNumber"] == "+18005550100"
    assert body["aiTransfer"] is True
    assert body["extracted"] == {
        "first_name": "Pat",
        "property_address": "12 Oak Street",
        "asking_price": 185000,
        "owns_property": True,
        "notes": None,
    }
    assert body["inboundRouting"] == {"did": "+18005550100"}
    assert body["nodesVisited"] == ["start", "qualify", "transfer"]
    assert body["startedAt"] == "2026-11-01T15:00:00+00:00"
    assert body["endedAt"] == "2026-11-01T15:01:35.500000+00:00"
    assert body["durationSeconds"] == 95.5
    assert body["recordingUrl"].endswith(
        "/api/v1/public/download/workflow/tok/recording"
    )
    assert body["transcriptUrl"].endswith(
        "/api/v1/public/download/workflow/tok/transcript"
    )
    # Engine bookkeeping never goes.
    assert "_secret" not in body["extracted"]
    assert "transfer_attempts" not in body["extracted"]


def test_an_outbound_call_carries_the_number_dialed():
    body = build_lead_intel_payload(
        _run(
            call_type="outbound",
            initial_context={
                "caller_number": "+18005550100",
                "called_number": "+18655559876",
            },
            gathered_context={},
        )
    )
    assert body["callType"] == "outbound"
    assert body["customerNumber"] == "+18655559876"
    assert body["ourNumber"] == "+18005550100"
    assert body["aiTransfer"] is False
    assert body["extracted"] == {}


def test_the_body_is_valid_json_within_hoppwhistles_limits():
    long = "x" * 5000
    body = build_lead_intel_payload(
        _run(
            gathered_context={
                "extracted_variables": {"condition": long},
                "call_tags": [f"t{i}" for i in range(80)],
            }
        )
    )
    json.dumps(body)
    assert len(body["extracted"]["condition"]) == 2000
    assert len(body["callTags"]) == 50


@pytest.mark.asyncio
async def test_delivery_goes_to_the_lead_intel_path_signed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ts"] = request.headers["X-Hopwhistle-Timestamp"]
        seen["sig"] = request.headers["X-Hopwhistle-Signature"]
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"data": {"status": "accepted"}})

    result = await deliver_report(
        base_url="https://hw.test/",
        secret="s3cret",
        payload=build_lead_intel_payload(_run()),
        path=LEAD_INTEL_PATH,
        transport=httpx.MockTransport(handler),
    )
    assert result.ok
    assert seen["url"] == "https://hw.test/api/internal/lead-intel/dograh/runs"
    assert seen["sig"] == sign("s3cret", seen["ts"], seen["body"])
