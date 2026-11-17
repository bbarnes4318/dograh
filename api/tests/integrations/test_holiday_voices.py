"""Holiday Voices integration: tool catalog, the four agent workflows, the
idempotent installer, and the post-call report.

Contract: docs/integrations/holiday-voices.mdx here, and
docs/holiday-voices/AI_VOICE_SPECIFICATION.md in Hoppwhistle.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.schemas.tool import CreateToolRequest
from api.services.integrations.base import IntegrationCompletionContext
from api.services.integrations.holiday_voices import installer as installer_mod
from api.services.integrations.holiday_voices.client import deliver_report
from api.services.integrations.holiday_voices.completion import (
    build_report,
    run_completion,
)
from api.services.integrations.holiday_voices.installer import (
    CREDENTIAL_NAME,
    HolidayVoicesInstallRequest,
    InstallError,
    install_holiday_voices,
)
from api.services.integrations.holiday_voices.node import HolidayVoicesNodeData
from api.services.integrations.holiday_voices.templates import (
    ALL_AGENTS,
    INBOUND,
    WORKFLOW_NAMES,
    build_workflow,
)
from api.services.integrations.holiday_voices.tool_catalog import (
    DISPOSITIONS,
    HTTP_TOOLS,
    TOOL_NAMES,
    build_tool_definitions,
)
from api.services.integrations.registry import get_package
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.tools.custom_tool import (
    custom_tool_function_name,
    execute_http_tool,
)
from api.services.workflow.workflow_graph import WorkflowGraph
from api.tests.conftest import MockToolModel

BASE = "https://agents.example.com"
KEY = "hvk_" + "a" * 40

#: The Hoppwhistle tool routes, exactly as VOICE_TOOL_NAMES in voice-tools.ts.
HOPPWHISTLE_TOOLS = {
    "getProducts", "getProductDetails", "checkInventory", "lookupCustomer",
    "createCustomer", "createOrderDraft", "addOrderItem", "updateOrderItem",
    "removeOrderItem", "getOrderSummary", "calculateShipping", "calculateTaxes",
    "applyValidPromotion", "createSecureCheckout", "sendCheckoutLink", "lookupOrder",
    "getTracking", "createSupportRequest", "requestHumanTransfer", "scheduleCallback",
}  # fmt: skip


# ── Registration and node ───────────────────────────────────────────────────


def test_package_registers_node_router_and_completion():
    package = get_package("holiday_voices")
    assert package is not None and package.run_completion is not None
    assert [n.type_name for n in package.nodes] == ["holiday_voices"]
    paths = [r.path for router in package.routers for r in router.routes]
    assert any(p.endswith("/integrations/holiday-voices/install") for p in paths)


def test_node_requires_url_and_credential_only_when_enabled():
    with pytest.raises(ValueError, match="missing required fields"):
        HolidayVoicesNodeData.model_validate({"name": "H"})
    with pytest.raises(ValueError, match="http"):
        HolidayVoicesNodeData.model_validate(
            {
                "name": "H",
                "holiday_voices_base_url": "ftp://x",
                "holiday_voices_credential_uuid": "c",
            }
        )
    assert HolidayVoicesNodeData.model_validate(
        {"name": "H", "holiday_voices_enabled": False}
    )


# ── Tools ───────────────────────────────────────────────────────────────────


def test_catalog_covers_every_hoppwhistle_tool_exactly_once():
    assert {route for route, _, _ in HTTP_TOOLS.values()} == HOPPWHISTLE_TOOLS
    assert len(HTTP_TOOLS) == len(HOPPWHISTLE_TOOLS)


def test_every_tool_is_a_valid_dograh_tool_with_a_stable_function_name():
    for spec in build_tool_definitions(BASE, "cred"):
        CreateToolRequest.model_validate(spec)
        # The name the LLM sees is the name the prompts use.
        assert custom_tool_function_name(spec["name"]) == spec["name"]
    assert [s["name"] for s in build_tool_definitions(BASE, "cred")] == list(TOOL_NAMES)


def test_http_tools_post_to_the_store_routes_with_hidden_session_presets():
    for spec in build_tool_definitions(BASE + "/", "cred"):
        if spec["category"] != "http_api":
            continue
        config = spec["definition"]["config"]
        route = HTTP_TOOLS[spec["name"]][0]
        assert config["url"] == f"{BASE}/api/internal/hv/voice/tools/{route}"
        assert config["credential_uuid"] == "cred"
        presets = {p["name"]: p["value_template"] for p in config["preset_parameters"]}
        assert presets == {
            "session_id": "{{initial_context.hv_session_id}}",
            "session_token": "{{initial_context.hv_session_token}}",
        }
        llm_params = {p["name"] for p in config.get("parameters") or []}
        # The model can never name a session, a tenant, a customer id or a price.
        assert not llm_params & {
            "session_id", "session_token", "tenant_id", "customer_id", "price",
            "unit_price_cents", "order_id",
        }  # fmt: skip


def test_transfer_uses_the_stores_resolver_and_never_a_fixed_number():
    spec = next(
        s
        for s in build_tool_definitions(BASE, "cred")
        if s["name"] == "transfer_to_team"
    )
    config = spec["definition"]["config"]
    assert config["destination_source"] == "dynamic"
    assert config["destination"] == ""
    assert config["resolver"]["url"] == f"{BASE}/api/internal/hv/voice/transfer"
    assert {p["name"] for p in config["resolver"]["preset_parameters"]} == {
        "session_id",
        "session_token",
    }


@pytest.mark.asyncio
async def test_the_llm_cannot_address_another_session():
    spec = next(
        s for s in build_tool_definitions(BASE, None) if s["name"] == "lookup_order"
    )
    tool = MockToolModel(
        tool_uuid="t1",
        name=spec["name"],
        description="",
        category="http_api",
        definition=spec["definition"],
    )
    with patch("api.services.workflow.tools.custom_tool.httpx.AsyncClient") as cls:
        client = AsyncMock()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": False, "instruction": "not verified"}
        client.request.return_value = response
        cls.return_value.__aenter__.return_value = client
        await execute_http_tool(
            tool,
            {"order_number": 1001, "session_id": "other", "session_token": "forged"},
            call_context_vars={"hv_session_id": "mine", "hv_session_token": "tok"},
        )
    body = client.request.call_args.kwargs["json"]
    assert body["session_id"] == "mine" and body["session_token"] == "tok"
    assert body["order_number"] == 1001


# ── Workflows ───────────────────────────────────────────────────────────────


def _tools() -> dict[str, str]:
    return {name: f"uuid-{name}" for name in TOOL_NAMES}


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_agent_workflows_are_valid_graphs(agent):
    definition = build_workflow(
        agent, _tools(), base_url=BASE, credential_uuid="cred-1"
    )
    graph = WorkflowGraph(ReactFlowDTO.model_validate(definition))
    assert graph.start_node_id == "greeting"
    nodes = {n["id"]: n for n in definition["nodes"]}
    assert "automated" in nodes["greeting"]["data"]["greeting"].lower()
    assert "never claim or imply to be one" in nodes["global"]["data"]["prompt"]
    assert "never take card numbers" in nodes["global"]["data"]["prompt"].lower()
    report = nodes["holiday_voices"]["data"]
    assert report["holiday_voices_agent"] == agent
    assert report["holiday_voices_credential_uuid"] == "cred-1"
    start = nodes["greeting"]["data"]
    if INBOUND[agent]:
        assert start["pre_call_fetch_url"] == f"{BASE}/api/internal/hv/voice/inbound"
        assert start["pre_call_fetch_credential_uuid"] == "cred-1"
    else:
        assert not start.get("pre_call_fetch_enabled")
    # Every node can end the call or reach a person; every tool ref is installed.
    installed = set(_tools().values())
    for node in definition["nodes"]:
        refs = node["data"].get("tool_uuids") or []
        assert set(refs) <= installed
    assert "uuid-end_call" in nodes["end"]["data"]["tool_uuids"]
    assert nodes["end"]["data"]["extraction_enabled"] is True
    # Prompts only name tools that exist.
    text = " ".join(
        str(n["data"].get(k) or "")
        for n in definition["nodes"]
        for k in ("prompt", "greeting")
    )
    for word in re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", text):
        if word.startswith(("get_", "check_", "create_", "send_", "lookup_", "schedule_", "request_", "transfer_", "calculate_", "apply_", "add_", "update_", "remove_")):  # fmt: skip
            assert word in TOOL_NAMES, word


def test_shopping_agent_uses_the_requested_opening():
    definition = build_workflow(
        "SHOPPING", _tools(), base_url=BASE, credential_uuid="c"
    )
    greeting = next(n for n in definition["nodes"] if n["id"] == "greeting")["data"]
    assert greeting["greeting"].startswith("Thanks for calling {{hv_store_name")
    assert "choose an ornament, place an order" in greeting["greeting"]


def test_wholesale_agent_never_quotes_terms():
    definition = build_workflow(
        "WHOLESALE", _tools(), base_url=BASE, credential_uuid="c"
    )
    qualify = next(n for n in definition["nodes"] if n["id"] == "qualify")["data"]
    assert "NEVER quote a wholesale price" in qualify["prompt"]
    end = next(n for n in definition["nodes"] if n["id"] == "end")["data"]
    assert {"business_name", "wholesale_quantity"} <= {
        v["name"] for v in end["extraction_variables"]
    }


# ── Installer ───────────────────────────────────────────────────────────────


class FakeDb:
    """Just enough of db_client, scoped by organization like the real one."""

    def __init__(self):
        self.ids = count(1)
        self.credentials: list[SimpleNamespace] = []
        self.tools: list[SimpleNamespace] = []
        self.workflows: list[SimpleNamespace] = []
        self.drafts: dict[int, dict] = {}

    async def get_credentials_for_organization(self, organization_id):
        return [c for c in self.credentials if c.organization_id == organization_id]

    async def create_credential(self, **kw):
        c = SimpleNamespace(credential_uuid=f"cred-{next(self.ids)}", **kw)
        self.credentials.append(c)
        return c

    async def update_credential(
        self, credential_uuid, organization_id, credential_data=None, **_
    ):
        for c in self.credentials:
            if (
                c.credential_uuid == credential_uuid
                and c.organization_id == organization_id
            ):
                c.credential_data = credential_data
                return c
        return None

    async def get_tools_for_organization(self, organization_id):
        return [t for t in self.tools if t.organization_id == organization_id]

    async def create_tool(self, **kw):
        t = SimpleNamespace(tool_uuid=f"tool-{next(self.ids)}", status="active", **kw)
        self.tools.append(t)
        return t

    async def update_tool(
        self, tool_uuid, organization_id, description=None, definition=None
    ):
        t = next(t for t in self.tools if t.tool_uuid == tool_uuid)
        assert t.organization_id == organization_id
        t.definition, t.description = definition, description
        return t

    async def get_all_workflows(self, organization_id=None, **_):
        return [w for w in self.workflows if w.organization_id == organization_id]

    async def create_workflow(
        self, name, workflow_definition, user_id, organization_id
    ):
        w = SimpleNamespace(
            id=next(self.ids),
            name=name,
            workflow_definition=workflow_definition,
            current_definition=SimpleNamespace(workflow_json=workflow_definition),
            organization_id=organization_id,
            workflow_uuid=f"wf-{name}",
            codes=[],
        )
        self.workflows.append(w)
        return w

    async def save_workflow_draft(self, workflow_id, workflow_definition=None):
        self.drafts[workflow_id] = workflow_definition

    async def add_call_disposition_code(self, workflow_id, code):
        next(w for w in self.workflows if w.id == workflow_id).codes.append(code)


@pytest.fixture
def fake_db():
    db = FakeDb()
    with patch.object(installer_mod, "db_client", db):
        yield db


@pytest.mark.asyncio
async def test_install_creates_everything_once_and_rerunning_duplicates_nothing(
    fake_db,
):
    request = HolidayVoicesInstallRequest(base_url=BASE, engine_key=KEY)
    first = await install_holiday_voices(request, organization_id=5, user_id=9)
    assert first.credential == "created"
    assert sorted(first.created) == sorted(TOOL_NAMES)
    assert [w["action"] for w in first.workflows] == ["created"] * 4
    assert {w.name for w in fake_db.workflows} == set(WORKFLOW_NAMES.values())
    assert fake_db.workflows[0].codes == list(DISPOSITIONS)

    second = await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE + "/v2"), organization_id=5, user_id=9
    )
    assert second.credential == "reused"
    assert second.created == [] and sorted(second.updated) == sorted(TOOL_NAMES)
    assert [w["action"] for w in second.workflows] == ["kept"] * 4
    assert len(fake_db.tools) == len(TOOL_NAMES)
    assert len(fake_db.workflows) == 4 and len(fake_db.credentials) == 1
    # Tools were repointed in place.
    assert all(
        "/v2/api/internal/hv/voice/" in json.dumps(t.definition)
        for t in fake_db.tools
        if t.category != "end_call"
    )


@pytest.mark.asyncio
async def test_a_renamed_customised_agent_is_found_and_never_overwritten(fake_db):
    await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE, engine_key=KEY),
        organization_id=5,
        user_id=9,
    )
    shop = next(w for w in fake_db.workflows if "Shopping" in w.name)
    shop.name = "Our phone line (edited)"
    shop.current_definition.workflow_json["nodes"][0]["data"]["prompt"] = "custom"
    kept = await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE), organization_id=5, user_id=9
    )
    assert len(fake_db.workflows) == 4
    assert (
        next(w for w in kept.workflows if w["agent"] == "SHOPPING")["action"] == "kept"
    )
    assert (
        shop.current_definition.workflow_json["nodes"][0]["data"]["prompt"] == "custom"
    )
    refreshed = await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE, refresh_workflows=True),
        organization_id=5,
        user_id=9,
    )
    # A refresh is only ever a draft; the published custom version keeps running.
    assert {w["action"] for w in refreshed.workflows} == {"draft_saved"}
    assert shop.id in fake_db.drafts
    assert (
        shop.current_definition.workflow_json["nodes"][0]["data"]["prompt"] == "custom"
    )


@pytest.mark.asyncio
async def test_a_rotated_key_updates_the_one_credential(fake_db):
    await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE, engine_key=KEY),
        organization_id=5,
        user_id=9,
    )
    new_key = "hvk_" + "b" * 40
    result = await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE, engine_key=new_key),
        organization_id=5,
        user_id=9,
    )
    assert result.credential == "updated"
    assert len(fake_db.credentials) == 1
    assert fake_db.credentials[0].credential_data == {"token": new_key}
    assert fake_db.credentials[0].name == CREDENTIAL_NAME


@pytest.mark.asyncio
async def test_never_installs_into_the_insurance_workspace(fake_db):
    fake_db.tools.append(
        SimpleNamespace(
            tool_uuid="ins-1",
            organization_id=1,
            name="lookup_customer",
            category="http_api",
            status="active",
            definition={
                "config": {"url": f"{BASE}/api/internal/ai-agent/tools/lookup_customer"}
            },
        )
    )
    with pytest.raises(InstallError) as exc:
        await install_holiday_voices(
            HolidayVoicesInstallRequest(base_url=BASE, engine_key=KEY),
            organization_id=1,
            user_id=1,
        )
    assert exc.value.status_code == 409
    assert fake_db.credentials == [] and len(fake_db.tools) == 1
    # Another organization's install never sees or touches org 1's rows.
    result = await install_holiday_voices(
        HolidayVoicesInstallRequest(base_url=BASE, engine_key=KEY),
        organization_id=5,
        user_id=9,
    )
    assert "lookup_customer" in result.created
    assert next(t for t in fake_db.tools if t.tool_uuid == "ins-1").organization_id == 1


@pytest.mark.asyncio
async def test_without_a_key_on_file_the_install_says_how_to_get_one(fake_db):
    with pytest.raises(InstallError, match="engine-key"):
        await install_holiday_voices(
            HolidayVoicesInstallRequest(base_url=BASE), organization_id=5, user_id=9
        )


def test_only_engine_keys_are_accepted():
    with pytest.raises(ValueError, match="hvk_"):
        HolidayVoicesInstallRequest(
            base_url=BASE, engine_key="some-insurance-secret-value"
        )


# ── Post-call report ────────────────────────────────────────────────────────


def _context(
    initial: dict, public_token: str | None = "pub"
) -> IntegrationCompletionContext:
    run = SimpleNamespace(
        initial_context=initial,
        gathered_context={
            "call_disposition": "checkout_link_sent",
            "extracted_variables": {
                "call_intent": "shopping",
                "units_discussed": 5,
                "caller_frustrated": False,
            },
            "transfer_attempts": [],
            "nodes_visited": ["greeting", "shop"],
        },
        usage_info={"call_duration_seconds": 184.4, "latency": {"avg_seconds": 0.91}},
        recording_url="s3://bucket/rec.wav",
        transcript_url="s3://bucket/t.txt",
        transcript_text="secret transcript",
        workflow_id=77,
        created_at=datetime(2026, 11, 17, 15, 0, tzinfo=UTC),
        annotations={"qa_1": {"summary": "Bought five ornaments for grandchildren."}},
        logs={
            "realtime_feedback_events": [
                {
                    "type": "rtf-function-call-start",
                    "payload": {"function_name": "get_products"},
                },
                {"type": "rtf-bot-text", "payload": {"text": "hi"}},
                {
                    "type": "rtf-function-call-start",
                    "payload": {"function_name": "add_order_item"},
                },
            ]
        },
    )
    return IntegrationCompletionContext(
        workflow_run_id=4321,
        workflow_run=run,
        workflow_definition={},
        definition_id=1,
        organization_id=5,
        public_token=public_token,
    )


def _node(**extra) -> HolidayVoicesNodeData:
    return HolidayVoicesNodeData.model_validate(
        {
            "name": "HV",
            "holiday_voices_base_url": BASE,
            "holiday_voices_credential_uuid": "cred-1",
            "holiday_voices_agent": "SHOPPING",
            **extra,
        }
    )


def test_report_carries_the_calls_facts_and_links_not_storage_keys():
    report = build_report(
        _context({"hv_session_id": "s1", "hv_session_token": "t1"}),
        _node(holiday_voices_cost_per_minute_cents=9),
    )
    assert report["run_id"] == "4321" and report["agent"] == "SHOPPING"
    assert report["outcome"] == "checkout_link_sent"
    assert report["duration_seconds"] == 184
    assert report["cost_cents"] == 28  # 184.4 s at 9¢/min
    assert report["latency_ms_avg"] == 910
    assert report["summary"] == "Bought five ornaments for grandchildren."
    assert report["tools_used"] == ["get_products", "add_order_item"]
    assert report["extracted"]["units_discussed"] == 5
    assert report["recording_url"].endswith("/workflow/pub/recording")
    assert "s3://" not in json.dumps(report)
    assert "secret transcript" not in json.dumps(report)


def test_without_a_public_token_no_link_is_sent():
    report = build_report(
        _context({"hv_session_id": "s", "hv_session_token": "t"}, public_token=None),
        _node(),
    )
    assert report["recording_url"] is None and report["transcript_url"] is None
    assert report["cost_cents"] is None


def test_a_run_the_store_did_not_open_is_skipped():
    assert build_report(_context({"hw_session_id": "insurance"}), _node()) is None


@pytest.mark.asyncio
async def test_delivery_uses_the_engine_key_and_retries_once_on_5xx():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"ok": True})

    result = await deliver_report(
        url=f"{BASE}/api/internal/hv/voice/runs/complete",
        auth_headers={"Authorization": f"Bearer {KEY}"},
        payload={"session_id": "s"},
        transport=httpx.MockTransport(handler),
    )
    assert result.ok and len(calls) == 2
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.asyncio
async def test_delivery_does_not_retry_a_refusal():
    calls = []
    result = await deliver_report(
        url=f"{BASE}/x",
        auth_headers={},
        payload={},
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(401)),
    )
    assert not result.ok and result.status_code == 401 and len(calls) == 1


@pytest.mark.asyncio
async def test_run_completion_resolves_the_credential_in_the_runs_own_org():
    context = _context({"hv_session_id": "s1", "hv_session_token": "secret-token"})
    lookup = AsyncMock(return_value={"Authorization": f"Bearer {KEY}"})
    with (
        patch(
            "api.services.integrations.holiday_voices.completion._auth_headers", lookup
        ),
        patch(
            "api.services.integrations.holiday_voices.completion.deliver_report",
            AsyncMock(
                return_value=SimpleNamespace(ok=True, status_code=200, detail="")
            ),
        ) as deliver,
    ):
        results = await run_completion(
            [
                {"id": "a", "data": _node().model_dump()},
                {"id": "b", "data": {"name": "x", "holiday_voices_enabled": False}},
            ],
            context,
        )
    lookup.assert_awaited_once_with("cred-1", 5)
    assert (
        deliver.await_args.kwargs["url"]
        == f"{BASE}/api/internal/hv/voice/runs/complete"
    )
    assert results["holiday_voices_a"]["delivered"] is True
    assert "holiday_voices_b" not in results
    assert "secret-token" not in json.dumps(results) and KEY not in json.dumps(results)


@pytest.mark.asyncio
async def test_run_completion_without_its_credential_reports_nothing():
    with patch(
        "api.services.integrations.holiday_voices.completion._auth_headers",
        AsyncMock(return_value=None),
    ):
        results = await run_completion(
            [{"id": "a", "data": _node().model_dump()}],
            _context({"hv_session_id": "s", "hv_session_token": "t"}),
        )
    assert results["holiday_voices_a"] == {
        "delivered": False,
        "error": "credential_not_found",
    }


# ── Real database, through the HTTP route ───────────────────────────────────


async def _org_user(db, provider: str):
    user, _ = await db.get_or_create_user_by_provider_id(provider)
    org, _ = await db.get_or_create_organization_by_provider_id(
        f"org-{provider}", user.id
    )
    await db.update_user_selected_organization(user.id, org.id)
    user.selected_organization_id = org.id
    return user, org


@pytest.mark.asyncio
async def test_install_route_against_the_real_database_is_idempotent(
    test_client_factory, db_session
):
    store, store_org = await _org_user(db_session, "hv-store-user")
    other, other_org = await _org_user(db_session, "insurance-user")
    other_tools_before = await db_session.get_tools_for_organization(other_org.id)

    body = {"base_url": BASE, "engine_key": KEY}
    async with test_client_factory(store) as client:
        first = await client.post(
            "/api/v1/integrations/holiday-voices/install", json=body
        )
        assert first.status_code == 200, first.text
        second = await client.post(
            "/api/v1/integrations/holiday-voices/install", json={"base_url": BASE}
        )
        assert second.status_code == 200, second.text

    one, two = first.json(), second.json()
    assert one["organization_id"] == store_org.id
    assert one["credential"] == "created" and two["credential"] == "reused"
    assert one["tools"] == two["tools"]
    assert [w["action"] for w in two["workflows"]] == ["kept"] * 4
    assert {w["workflow_id"] for w in one["workflows"]} == {
        w["workflow_id"] for w in two["workflows"]
    }

    tools = await db_session.get_tools_for_organization(store_org.id)
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    workflows = await db_session.get_all_workflows(organization_id=store_org.id)
    assert sorted(w.name for w in workflows) == sorted(WORKFLOW_NAMES.values())
    shop = next(w for w in workflows if "Shopping" in w.name)
    graph = WorkflowGraph(ReactFlowDTO.model_validate(shop.workflow_definition))
    start = graph.nodes[graph.start_node_id]
    assert start.pre_call_fetch_credential_uuid == one["credential_uuid"]
    # The credential is the store's, and only the store's.
    assert await db_session.get_credential_by_uuid(one["credential_uuid"], store_org.id)
    assert not await db_session.get_credential_by_uuid(
        one["credential_uuid"], other_org.id
    )
    assert (
        await db_session.get_tools_for_organization(other_org.id) == other_tools_before
    )
    assert other.selected_organization_id != store_org.id


@pytest.mark.asyncio
async def test_cli_installs_into_the_provider_users_org_only(db_session, capsys):
    from api.services.integrations.holiday_voices import cli

    store, store_org = await _org_user(db_session, "hoppwhistle-holiday-voices-test")
    args = SimpleNamespace(
        provider_id="hoppwhistle-holiday-voices-test",
        base_url=BASE,
        engine_key=KEY,
        refresh_workflows=False,
        tools_only=False,
        cost_per_minute_cents=11.0,
        outbound_api_key=True,
        rotate_outbound_api_key=False,
    )
    assert await cli._main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["organization_id"] == store_org.id
    assert len(out["workflows"]) == 4
    first_key = out["outbound_api_key"]
    assert first_key["action"] == "created" and first_key["api_key"]
    # The key opens this organization only.
    validated = await db_session.validate_api_key(first_key["api_key"])
    assert validated.organization_id == store_org.id

    # Re-running keeps the key (and cannot show it again); rotating replaces it.
    assert await cli._main(args) == 0
    again = json.loads(capsys.readouterr().out)["outbound_api_key"]
    assert again == {
        "action": "kept",
        "key_prefix": first_key["key_prefix"],
        "api_key": None,
    }
    rotate = SimpleNamespace(**{**vars(args), "rotate_outbound_api_key": True})
    assert await cli._main(rotate) == 0
    rotated = json.loads(capsys.readouterr().out)["outbound_api_key"]
    assert rotated["action"] == "rotated" and rotated["api_key"] != first_key["api_key"]
    assert await db_session.validate_api_key(first_key["api_key"]) is None
    assert (
        await db_session.validate_api_key(rotated["api_key"])
    ).organization_id == store_org.id
    missing = SimpleNamespace(**{**vars(args), "provider_id": "nobody"})
    assert await cli._main(missing) == 2
