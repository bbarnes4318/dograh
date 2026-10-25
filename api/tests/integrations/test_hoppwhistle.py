"""Hoppwhistle integration: tools, templates, the post-call report, and the
two generic engine changes it relies on (resolver declines, transfer-attempt
recording).

The contract these pin is documented in docs/integrations/hoppwhistle.mdx and,
on the Hoppwhistle side, docs/ai-agents/DOGRAH_CONTRACT.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.schemas.tool import CreateToolRequest
from api.services.configuration.masking import mask_workflow_definition
from api.services.integrations.base import IntegrationCompletionContext
from api.services.integrations.hoppwhistle.client import deliver_report, sign
from api.services.integrations.hoppwhistle.completion import (
    build_report,
    run_completion,
)
from api.services.integrations.hoppwhistle.node import NODE, HoppwhistleNodeData
from api.services.integrations.hoppwhistle.templates import build_workflow
from api.services.integrations.hoppwhistle.tool_catalog import (
    CONTRACT_VERSION,
    TOOL_NAMES,
    build_tool_definitions,
)
from api.services.integrations.registry import get_package
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.tools.custom_tool import execute_http_tool
from api.services.workflow.tools.transfer_resolver import (
    TransferResolutionError,
    _resolve_from_response,
)
from api.services.workflow.workflow_graph import WorkflowGraph
from api.tests.conftest import MockToolModel

BASE = "https://agents.example.com"
SECRET = "hoppwhistle-shared-secret-for-tests"


# ── Node and registration ───────────────────────────────────────────────────


def test_package_registers_node_router_and_completion():
    package = get_package("hoppwhistle")
    assert package is not None
    assert package.run_completion is not None
    assert [node.type_name for node in package.nodes] == ["hoppwhistle"]
    assert any(
        route.path.endswith("/install")
        for router in package.routers
        for route in router.routes
    )


def test_node_requires_url_and_secret_only_when_enabled():
    with pytest.raises(ValueError, match="missing required fields"):
        HoppwhistleNodeData.model_validate({"name": "H", "hoppwhistle_enabled": True})
    with pytest.raises(ValueError, match="http"):
        HoppwhistleNodeData.model_validate(
            {
                "name": "H",
                "hoppwhistle_base_url": "ftp://x",
                "hoppwhistle_signing_secret": "s" * 20,
            }
        )
    assert HoppwhistleNodeData.model_validate(
        {"name": "H", "hoppwhistle_enabled": False}
    )


def test_signing_secret_is_masked_on_read():
    assert NODE.sensitive_fields == ("hoppwhistle_signing_secret",)
    masked = mask_workflow_definition(
        {
            "nodes": [
                {
                    "id": "h",
                    "type": "hoppwhistle",
                    "data": {
                        "hoppwhistle_signing_secret": SECRET,
                        "hoppwhistle_base_url": BASE,
                    },
                }
            ]
        }
    )
    assert masked["nodes"][0]["data"]["hoppwhistle_signing_secret"] != SECRET
    assert masked["nodes"][0]["data"]["hoppwhistle_base_url"] == BASE


# ── Tool catalog ────────────────────────────────────────────────────────────


def test_every_tool_is_a_valid_dograh_tool():
    definitions = build_tool_definitions(BASE + "/", "cred-1")
    assert [d["name"] for d in definitions] == list(TOOL_NAMES)
    for spec in definitions:
        CreateToolRequest.model_validate(spec)


def test_http_tools_post_to_hoppwhistle_with_hidden_session_presets():
    for spec in build_tool_definitions(BASE, "cred-1"):
        config = spec["definition"]["config"]
        if spec["category"] == "http_api":
            assert config["url"] == f"{BASE}/api/internal/ai-agent/tools/{spec['name']}"
            assert config["credential_uuid"] == "cred-1"
            presets = {
                p["name"]: p["value_template"] for p in config["preset_parameters"]
            }
        elif spec["category"] == "transfer_call":
            assert config["destination_source"] == "dynamic"
            assert config["resolver"]["url"] == f"{BASE}/api/internal/ai-agent/transfer"
            presets = {
                p["name"]: p["value_template"]
                for p in config["resolver"]["preset_parameters"]
            }
        else:
            continue
        assert presets == {
            "session_id": "{{initial_context.hw_session_id}}",
            "session_token": "{{initial_context.hw_session_token}}",
        }
        # The LLM is never offered a session parameter to fill in.
        llm_params = (
            config.get("parameters")
            or config.get("resolver", {}).get("parameters")
            or []
        )
        assert not {p["name"] for p in llm_params} & {
            "session_id",
            "session_token",
            "tenant_id",
        }


@pytest.mark.asyncio
async def test_the_llm_cannot_address_another_session():
    spec = next(
        s for s in build_tool_definitions(BASE, None) if s["name"] == "mark_dnc"
    )
    tool = MockToolModel(
        tool_uuid="t1",
        name=spec["name"],
        description="",
        category="http_api",
        definition=spec["definition"],
    )
    with patch(
        "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
    ) as client_class:
        client = AsyncMock()
        response = Mock(status_code=200)
        response.json.return_value = {
            "ok": True,
            "instruction": "end_call_dnc_confirmed",
        }
        client.request.return_value = response
        client_class.return_value.__aenter__.return_value = client
        await execute_http_tool(
            tool,
            {
                "reason": "stop",
                "session_id": "someone-elses",
                "session_token": "forged",
            },
            call_context_vars={"hw_session_id": "mine", "hw_session_token": "tok"},
        )
    body = client.request.call_args.kwargs["json"]
    assert body["session_id"] == "mine"
    assert body["session_token"] == "tok"


# ── Templates ───────────────────────────────────────────────────────────────


def _tools() -> dict[str, str]:
    return {name: f"uuid-{name}" for name in TOOL_NAMES}


@pytest.mark.parametrize(
    "product,inbound",
    [
        ("CALLBACK_RECOVERY", False),
        ("CALLBACK_RECOVERY", True),
        ("LEAD_QUALIFIER", False),
    ],
)
def test_templates_are_valid_workflows(product, inbound):
    definition = build_workflow(
        product,
        _tools(),
        base_url=BASE,
        signing_secret=SECRET,
        inbound=inbound,
        credential_uuid="cred-1",
    )
    graph = WorkflowGraph(ReactFlowDTO.model_validate(definition))
    assert graph.start_node_id == "greeting"
    nodes = {n["id"]: n for n in definition["nodes"]}
    # The rules every node shares live in the global node; qualification only asks what Hoppwhistle says.
    assert "NOT a licensed insurance agent" in nodes["global"]["data"]["prompt"]
    assert "virtual assistant" in nodes["greeting"]["data"]["greeting"]
    assert "uuid-transfer_to_agent" in nodes["qualify"]["data"]["tool_uuids"]
    assert "uuid-mark_dnc" in nodes["greeting"]["data"]["tool_uuids"]
    assert nodes["hoppwhistle"]["data"]["hoppwhistle_base_url"] == BASE
    if inbound:
        assert nodes["greeting"]["data"]["pre_call_fetch_enabled"] is True
        assert (
            nodes["greeting"]["data"]["pre_call_fetch_url"]
            == f"{BASE}/api/internal/ai-agent/inbound"
        )
    else:
        assert not nodes["greeting"]["data"].get("pre_call_fetch_enabled")


# ── Post-call report ────────────────────────────────────────────────────────


def _context(initial_context: dict, **run_fields) -> IntegrationCompletionContext:
    run = SimpleNamespace(
        initial_context=initial_context,
        gathered_context=run_fields.pop(
            "gathered_context",
            {
                "call_disposition": "TRANSFER_CALL",
                "call_tags": ["TRANSFER_CALL"],
                "extracted_variables": {"callback_time": "after 5"},
                "transfer_attempts": [
                    {"status": "transfer_success", "reason": None, "at": "x"}
                ],
                "nodes_visited": ["greeting"],
            },
        ),
        usage_info={
            "call_duration_seconds": 125,
            "latency": {"avg_seconds": 0.84},
            "llm": {"m": {}},
        },
        transcript_text="assistant: Hi\nuser: Hello",
        recording_url="s3://bucket/rec.wav",
        transcript_url="s3://bucket/t.txt",
        workflow_id=42,
        created_at=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        **run_fields,
    )
    return IntegrationCompletionContext(
        workflow_run_id=981,
        workflow_run=run,
        workflow_definition={},
        definition_id=1,
        organization_id=7,
        public_token="pub-token",
    )


def _node(**extra) -> HoppwhistleNodeData:
    return HoppwhistleNodeData.model_validate(
        {
            "name": "H",
            "hoppwhistle_base_url": BASE,
            "hoppwhistle_signing_secret": SECRET,
            **extra,
        }
    )


def test_report_carries_the_conversation_facts_and_nothing_internal():
    report = build_report(
        _context({"hw_session_id": "s1", "hw_session_token": "t1"}),
        _node(hoppwhistle_cost_per_minute_cents=12),
    )
    assert report["contract_version"] == CONTRACT_VERSION
    assert report["session_id"] == "s1" and report["external_run_id"] == "981"
    assert report["disposition"] == "TRANSFER_CALL"
    assert report["transfer_attempts"] == [
        {"status": "transfer_success", "reason": None, "at": "x"}
    ]
    assert report["extracted"] == {"callback_time": "after 5"}
    assert report["latency_ms_avg"] == 840
    assert report["cost_cents"] == 25.0
    assert report["recording_url"].endswith("/pub-token/recording")
    assert report["ended_at"] == "2026-10-24T15:02:05+00:00"
    assert report["voicemail"] is False


def test_voicemail_is_flagged():
    context = _context(
        {"hw_session_id": "s1", "hw_session_token": "t1"},
        gathered_context={"call_disposition": "voicemail_detected"},
    )
    assert build_report(context, _node())["voicemail"] is True


def test_a_run_hoppwhistle_did_not_start_is_skipped():
    assert build_report(_context({"phone_number": "+15125550199"}), _node()) is None


def test_signature_matches_hoppwhistles_scheme():
    expected = hmac.new(SECRET.encode(), b"1700000000.{}", hashlib.sha256).hexdigest()
    assert sign(SECRET, "1700000000", "{}") == f"v1={expected}"


@pytest.mark.asyncio
async def test_delivery_is_signed_and_retried_once_on_a_server_error():
    seen: list[httpx.Request] = []
    statuses = iter([503, 201])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(statuses), json={})

    result = await deliver_report(
        base_url=BASE,
        secret=SECRET,
        payload={"session_id": "s1"},
        transport=httpx.MockTransport(handler),
    )
    assert result.ok and len(seen) == 2
    request = seen[-1]
    assert str(request.url) == f"{BASE}/api/internal/ai-agent/runs/complete"
    body = request.content.decode()
    timestamp = request.headers["x-hopwhistle-timestamp"]
    assert request.headers["x-hopwhistle-signature"] == sign(SECRET, timestamp, body)
    assert SECRET not in body and SECRET not in json.dumps(dict(request.headers))


@pytest.mark.asyncio
async def test_delivery_does_not_retry_a_refusal():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": "bad signature"})

    result = await deliver_report(
        base_url=BASE, secret=SECRET, payload={}, transport=httpx.MockTransport(handler)
    )
    assert not result.ok and result.status_code == 401 and len(calls) == 1


@pytest.mark.asyncio
async def test_run_completion_skips_disabled_nodes_and_never_echoes_the_token():
    context = _context({"hw_session_id": "s1", "hw_session_token": "secret-token"})
    with patch(
        "api.services.integrations.hoppwhistle.completion.deliver_report",
        AsyncMock(
            return_value=SimpleNamespace(ok=True, status_code=201, detail="delivered")
        ),
    ) as deliver:
        results = await run_completion(
            [
                {
                    "id": "h1",
                    "data": {
                        "name": "H",
                        "hoppwhistle_base_url": BASE,
                        "hoppwhistle_signing_secret": SECRET,
                    },
                },
                {"id": "h2", "data": {"name": "H", "hoppwhistle_enabled": False}},
            ],
            context,
        )
    assert deliver.await_count == 1
    assert results["hoppwhistle_h1"]["delivered"] is True
    assert "hoppwhistle_h2" not in results
    assert "secret-token" not in json.dumps(results)


# ── Generic engine changes ──────────────────────────────────────────────────


def test_resolver_may_decline_with_a_reason_and_what_to_say():
    with pytest.raises(TransferResolutionError) as caught:
        _resolve_from_response(
            response_data={
                "transfer_context": {
                    "destination": None,
                    "decline_reason": "No Licensed Agent",
                    "message": "Offer a callback from a licensed agent.",
                }
            },
            config={"timeout": 30},
            resolution_id="r1",
        )
    assert caught.value.reason == "declined_no_licensed_agent"
    assert caught.value.llm_message == "Offer a callback from a licensed agent."


def test_a_missing_destination_without_a_decline_is_still_an_error():
    with pytest.raises(TransferResolutionError) as caught:
        _resolve_from_response(
            response_data={"transfer_context": {}}, config={}, resolution_id="r1"
        )
    assert caught.value.reason == "no_destination"
    assert caught.value.llm_message is None


@pytest.mark.asyncio
async def test_transfer_outcomes_are_kept_in_gathered_context():
    from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

    engine = Mock()
    engine._gathered_context = {}
    engine._transfer_handoff_started = True
    engine.end_call_with_reason = AsyncMock()
    manager = CustomToolManager(engine)
    params = Mock()
    params.result_callback = AsyncMock()

    await manager._handle_transfer_result(
        {
            "action": "transfer_failed",
            "status": "failed",
            "reason": "declined_no_agent_available",
        },
        params,
        None,
    )
    await manager._handle_transfer_result(
        {"action": "destination_answered", "status": "success", "conference_id": "c"},
        params,
        None,
    )
    attempts = engine._gathered_context["transfer_attempts"]
    assert [(a["status"], a["reason"]) for a in attempts] == [
        ("transfer_failed", "declined_no_agent_available"),
        ("transfer_success", None),
    ]
