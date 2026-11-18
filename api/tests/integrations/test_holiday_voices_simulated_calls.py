"""Simulated calls through the four Holiday Voices agents.

Each test runs the REAL PipecatEngine on the agent's real workflow (built by
templates.build_workflow, exactly what the installer stores), with its real
tool definitions (tool_catalog.build_tool_definitions). Only the edges of the
world are simulated:

  - the caller and the model: a scripted MockLLM that calls tools and takes
    transitions in the order a real conversation would;
  - speech and the phone line: MockTTS and MockTransport;
  - Hoppwhistle: a fake HTTP server that records every tool request and answers
    like the real /api/internal/hv/voice/tools/<tool> routes.

What is checked: the call moves through the right nodes, each node offers the
model only its own tools, every tool request reaches the right Hoppwhistle
route with the session id/token from the call context (never from the model),
the store's engine key as the bearer credential, and the model's arguments.

Real telephone calls are NOT covered here; that stays an activation check
until an approved number is assigned to the store's workspace.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pipecat.frames.frames import LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
)
from pipecat.tests import ContextCapturingMockLLM, MockLLMService, MockTTSService
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams

from api.services.integrations.holiday_voices.templates import build_workflow
from api.services.integrations.holiday_voices.tool_catalog import (
    HTTP_TOOLS,
    TOOL_NAMES,
    build_tool_definitions,
)
from api.services.pipecat.worker_runner import run_pipeline_worker
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph
from api.tests.conftest import MockToolModel

BASE = "https://agents.example.com"
ENGINE_KEY = "hvk_" + "s" * 40
SESSION = {"hv_session_id": "sess-123", "hv_session_token": "tok-abc"}


class FakeHoppwhistle:
    """Answers like /api/internal/hv/voice/tools/<tool>, and remembers every request."""

    ANSWERS: dict[str, dict[str, Any]] = {
        "getProducts": {
            "ok": True,
            "products": [
                {
                    "name": "Recordable Ornament",
                    "options": [{"color": "Red", "price": "$30.00"}],
                }
            ],
            "instruction": "Describe the finishes; prices are $30.",
        },
        "addOrderItem": {
            "ok": True,
            "total": "$60.00",
            "instruction": "Confirm the items.",
        },
        "createSecureCheckout": {
            "ok": True,
            "url": "https://x/checkout/t",
            "instruction": "Offer to send the link.",
        },
        "sendCheckoutLink": {
            "ok": True,
            "sent": "email",
            "instruction": "Say it is on its way; not a sale.",
        },
        "lookupOrder": {
            "ok": True,
            "verified": True,
            "status": "shipped",
            "instruction": "Give the status.",
        },
        "createSupportRequest": {
            "ok": True,
            "instruction": "Tell them the team will follow up.",
        },
    }

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def client(self) -> Any:
        async def request(method: str, url: str, **kwargs: Any) -> Any:
            route = url.rsplit("/", 1)[-1]
            self.requests.append(
                {
                    "method": method,
                    "url": url,
                    "route": route,
                    "json": kwargs.get("json"),
                    "headers": kwargs.get("headers") or {},
                }
            )
            response = Mock(status_code=200)
            body = self.ANSWERS.get(route, {"ok": True, "instruction": "Continue."})
            response.json.return_value = body
            response.text = json.dumps(body)
            response.headers = {"content-type": "application/json"}
            return response

        client = AsyncMock()
        client.request.side_effect = request
        cm = AsyncMock()
        cm.__aenter__.return_value = client
        cm.__aexit__.return_value = False
        return cm

    def routes(self) -> list[str]:
        return [r["route"] for r in self.requests]


def _tools() -> list[MockToolModel]:
    return [
        MockToolModel(
            tool_uuid=f"uuid-{spec['name']}",
            name=spec["name"],
            description=spec["description"],
            category=spec["category"],
            definition=spec["definition"],
        )
        for spec in build_tool_definitions(BASE, "cred-hv")
    ]


def _graph(agent: str) -> WorkflowGraph:
    definition = build_workflow(
        agent,  # type: ignore[arg-type]
        {name: f"uuid-{name}" for name in TOOL_NAMES},
        base_url=BASE,
        credential_uuid="cred-hv",
    )
    return WorkflowGraph(ReactFlowDTO.model_validate(definition))


def _call(name: str, args: dict[str, Any] | None = None, i: int = 0) -> dict[str, Any]:
    return {"name": name, "arguments": args or {}, "tool_call_id": f"call_{name}_{i}"}


async def simulate(agent: str, script: list[list[dict[str, Any]]]):
    """Run one scripted call. Each script step is one model turn (its tool calls)."""
    steps = [
        MockLLMService.create_multiple_function_call_chunks(turn) for turn in script
    ]
    steps += [
        MockLLMService.create_text_chunks("Thanks, happy holidays!") for _ in range(4)
    ]
    llm = ContextCapturingMockLLM(mock_steps=steps, chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=20, frame_delay=0)
    transport = MockTransport(
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        )
    )
    context = LLMContext()
    aggregator = LLMContextAggregatorPair(
        context, assistant_params=LLMAssistantAggregatorParams()
    )
    workflow = _graph(agent)
    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=workflow,
        call_context_vars={
            **SESSION,
            "hv_store_name": "Holiday Voices",
            "hv_caller_known": False,
        },
        workflow_run_id=1,
    )
    pipeline = Pipeline([llm, tts, transport.output(), aggregator.assistant()])
    task = PipelineWorker(pipeline, params=PipelineParams(), enable_rtvi=False)
    engine.set_task(task)

    fake = FakeHoppwhistle()
    tools = _tools()
    visited: list[str] = []
    offered: dict[str, set[str]] = {}
    original_set_node = engine.set_node

    async def tracking_set_node(node_id: str, *a: Any, **kw: Any) -> Any:
        visited.append(node_id)
        result = await original_set_node(node_id, *a, **kw)
        offered[node_id] = set(getattr(llm, "_functions", {}).keys())
        return result

    engine.set_node = tracking_set_node  # type: ignore[method-assign]

    async def by_uuids(uuids: list[str], organization_id: int) -> list[MockToolModel]:
        assert organization_id == 4  # the store's own organization, never another
        return [t for t in tools if t.tool_uuid in set(uuids)]

    credential = SimpleNamespace(
        name="Holiday Voices engine key",
        credential_type="bearer_token",
        credential_data={"token": ENGINE_KEY},
    )

    with (
        patch(
            "api.db:db_client.get_organization_id_by_workflow_run_id",
            new_callable=AsyncMock,
            return_value=4,
        ),
        patch("api.db:db_client.get_tools_by_uuids", side_effect=by_uuids),
        patch(
            "api.db:db_client.get_credential_by_uuid",
            new_callable=AsyncMock,
            return_value=credential,
        ),
        patch(
            "api.services.workflow.tools.custom_tool.httpx.AsyncClient",
            side_effect=lambda *a, **k: fake.client(),
        ),
    ):

        async def run() -> None:
            await run_pipeline_worker(task)

        async def start() -> None:
            await asyncio.sleep(0.01)
            await engine.initialize()
            await engine.set_node(engine.workflow.start_node_id)
            await engine.llm.queue_frame(LLMContextFrame(engine.context))

        await asyncio.wait_for(asyncio.gather(run(), start()), timeout=30)

    return SimpleNamespace(
        llm=llm, fake=fake, visited=visited, offered=offered, engine=engine
    )


def assert_tool_requests_are_session_bound(fake: FakeHoppwhistle) -> None:
    assert fake.requests, "no tool reached Hoppwhistle"
    for r in fake.requests:
        assert r["method"] == "POST"
        assert r["url"].startswith(f"{BASE}/api/internal/hv/voice/tools/")
        assert r["headers"].get("Authorization") == f"Bearer {ENGINE_KEY}"
        assert r["json"]["session_id"] == SESSION["hv_session_id"]
        assert r["json"]["session_token"] == SESSION["hv_session_token"]


@pytest.mark.asyncio
async def test_shopping_call_builds_an_order_and_sends_the_checkout_link():
    sim = await simulate(
        "SHOPPING",
        [
            [_call("lookup_customer"), _call("get_products")],
            [_call("wants_to_shop")],
            [
                _call("add_order_item", {"color": "red", "quantity": 2}),
                _call("add_order_item", {"color": "gold", "quantity": 1}, 1),
            ],
            # The caller changes their mind: no gold after all.
            [_call("update_order_item", {"color": "gold", "quantity": 0})],
            [_call("get_order_summary")],
            [_call("ready_to_check_out")],
            [
                _call("calculate_shipping", {"state": "GA", "zip": "30301"}),
                _call(
                    "create_customer",
                    {
                        "first_name": "Avery",
                        "last_name": "Lane",
                        "email": "avery@example.com",
                    },
                ),
            ],
            [_call("create_secure_checkout")],
            [_call("send_checkout_link", {"channel": "email"})],
            [_call("link_sent_or_done")],
            [_call("end_call", {"reason": "checkout_link_sent"})],
        ],
    )
    assert sim.visited[:4] == ["greeting", "shop", "checkout", "end"]
    assert sim.fake.routes() == [
        "lookupCustomer",
        "getProducts",
        "addOrderItem",
        "addOrderItem",
        "updateOrderItem",
        "getOrderSummary",
        "calculateShipping",
        "createCustomer",
        "createSecureCheckout",
        "sendCheckoutLink",
    ]
    assert_tool_requests_are_session_bound(sim.fake)
    adds = [r["json"] for r in sim.fake.requests if r["route"] == "addOrderItem"]
    assert {(a["color"], a["quantity"]) for a in adds} == {("red", 2), ("gold", 1)}
    # The checkout node never offered order-status lookups; the greeting never offered checkout.
    assert "send_checkout_link" not in sim.offered["greeting"]
    assert "lookup_order" not in sim.offered["checkout"]


@pytest.mark.asyncio
async def test_support_call_checks_an_order_then_hands_a_problem_to_the_team():
    sim = await simulate(
        "SUPPORT",
        [
            [_call("order_status")],
            # The model tries to name another session: the preset wins.
            [
                _call(
                    "lookup_order",
                    {
                        "order_number": 1001,
                        "email": "avery@example.com",
                        "session_id": "forged",
                        "session_token": "forged",
                    },
                )
            ],
            [
                _call(
                    "get_tracking", {"order_number": 1001, "email": "avery@example.com"}
                )
            ],
            [_call("has_a_problem")],
            [_call("get_product_details", {"product": "ornament will not play"})],
            [
                _call(
                    "create_support_request",
                    {
                        "topic": "Ornament will not play",
                        "details": "Pressed, silent.",
                        "order_number": 1001,
                    },
                ),
                _call(
                    "request_human_transfer",
                    {"reason": "Wants a replacement", "summary": "Order 1001 silent"},
                ),
            ],
            [_call("resolved_or_handed_off")],
            [_call("end_call", {"reason": "support_request_created"})],
        ],
    )
    assert sim.visited[:4] == ["greeting", "status", "troubleshoot", "end"]
    assert sim.fake.routes() == [
        "lookupOrder",
        "getTracking",
        "getProductDetails",
        "createSupportRequest",
        "requestHumanTransfer",
    ]
    assert_tool_requests_are_session_bound(sim.fake)
    lookup = sim.fake.requests[0]["json"]
    assert lookup["order_number"] == 1001 and lookup["email"] == "avery@example.com"
    # Support never offers order-building or checkout tools.
    for node in ("status", "troubleshoot"):
        assert not sim.offered[node] & {
            "add_order_item",
            "create_secure_checkout",
            "send_checkout_link",
        }
    assert "transfer_to_team" in sim.offered["troubleshoot"]


@pytest.mark.asyncio
async def test_outbound_call_follows_up_and_sends_a_link():
    sim = await simulate(
        "OUTBOUND_SALES",
        [
            [_call("lookup_customer")],
            [_call("good_time")],
            [
                _call("get_products"),
                _call("add_order_item", {"color": "green", "quantity": 3}),
            ],
            [_call("ready")],
            [
                _call("calculate_shipping", {"state": "TX", "zip": "75001"}),
                _call(
                    "create_customer", {"first_name": "Sam", "email": "sam@example.com"}
                ),
            ],
            [
                _call("create_secure_checkout"),
                _call("send_checkout_link", {"channel": "email"}),
            ],
            [_call("link_sent")],
            [_call("end_call", {"reason": "checkout_link_sent"})],
        ],
    )
    assert sim.visited[:4] == ["greeting", "offer", "checkout", "end"]
    assert sim.fake.routes() == [
        "lookupCustomer",
        "getProducts",
        "addOrderItem",
        "calculateShipping",
        "createCustomer",
        "createSecureCheckout",
        "sendCheckoutLink",
    ]
    assert_tool_requests_are_session_bound(sim.fake)


@pytest.mark.asyncio
async def test_outbound_do_not_call_ends_without_touching_the_store():
    sim = await simulate(
        "OUTBOUND_SALES",
        [
            [_call("not_now")],
            [_call("end_call", {"reason": "do_not_call"})],
        ],
    )
    assert sim.visited[:2] == ["greeting", "end"]
    assert sim.fake.routes() == []


@pytest.mark.asyncio
async def test_wholesale_call_qualifies_and_hands_the_quote_to_a_person():
    sim = await simulate(
        "WHOLESALE",
        [
            [_call("business_inquiry")],
            [
                _call(
                    "get_product_details", {"product": "bulk ornaments for a hospice"}
                ),
                _call(
                    "create_customer",
                    {"first_name": "Dana", "email": "dana@hospice.example.org"},
                ),
            ],
            [
                _call(
                    "create_support_request",
                    {
                        "topic": "Wholesale inquiry",
                        "details": "Riverside Hospice, ~200 ornaments, by Dec 1",
                    },
                )
            ],
            [_call("qualified")],
            [
                _call(
                    "schedule_callback",
                    {"preferred_time": "tomorrow morning", "reason": "wholesale quote"},
                )
            ],
            [_call("handed_off")],
            [_call("end_call", {"reason": "wholesale_lead"})],
        ],
    )
    assert sim.visited[:4] == ["greeting", "qualify", "handoff", "end"]
    assert sim.fake.routes() == [
        "getProductDetails",
        "createCustomer",
        "createSupportRequest",
        "scheduleCallback",
    ]
    assert_tool_requests_are_session_bound(sim.fake)
    request = next(
        r["json"] for r in sim.fake.requests if r["route"] == "createSupportRequest"
    )
    assert request["topic"] == "Wholesale inquiry"
    # Wholesale never builds an order or quotes a price by itself.
    for node in ("qualify", "handoff"):
        assert not sim.offered[node] & {
            "add_order_item",
            "create_secure_checkout",
            "apply_valid_promotion",
        }


def test_every_http_tool_is_exercised_by_some_simulated_call():
    """Guard: a tool added to the catalog must be added to a simulated call too."""
    exercised = {
        "lookupCustomer", "getProducts", "addOrderItem", "updateOrderItem", "getOrderSummary",
        "calculateShipping", "createCustomer", "createSecureCheckout", "sendCheckoutLink",
        "lookupOrder", "getTracking", "getProductDetails", "createSupportRequest",
        "requestHumanTransfer", "scheduleCallback",
    }  # fmt: skip
    # Not driven by a scripted call (covered by Hoppwhistle's own route tests):
    unscripted = {
        "checkInventory",
        "createOrderDraft",
        "removeOrderItem",
        "calculateTaxes",
        "applyValidPromotion",
    }
    assert exercised | unscripted == {route for route, _, _ in HTTP_TOOLS.values()}
