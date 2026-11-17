"""The four Holiday Voices agents, as Dograh workflow definitions.

Built from Dograh's own node types: a ``globalNode`` with the store's rules,
a ``startCall`` greeting, ``agentNode`` stages, an ``endCall`` wrap-up that
extracts the call's facts, and a ``holiday_voices`` reporting node that posts
the finished call back to Hoppwhistle. The store edits them in the agent
studio like any workflow; the rules that matter (prices, stock, who may hear
an order's status, consent) are enforced by Hoppwhistle's tool answers, so a
prompt edit cannot drop them.

    SHOPPING        inbound   answers the store line, recommends, builds the
                              order, sends the secure checkout link
    SUPPORT         inbound   product and recording help, order status and
                              tracking (verified), support requests, people
    OUTBOUND_SALES  outbound  requested callbacks and opted-in follow-ups;
                              Hoppwhistle opens the session before dialling
    WHOLESALE       inbound   business inquiries: qualify, record, hand the
                              quote to a person

Prompts read only what Hoppwhistle put in ``initial_context``:
``hv_store_name``, ``hv_caller_known``, ``hv_customer_first_name``,
``hv_returning_customer``, ``hv_disclosure``, and for outbound calls
``hv_call_reason`` / ``hv_campaign_name``.
"""

from __future__ import annotations

from typing import Any, Literal

from .tool_catalog import DISPOSITIONS, pre_call_fetch_url

AgentKey = Literal["SHOPPING", "SUPPORT", "OUTBOUND_SALES", "WHOLESALE"]
ALL_AGENTS: tuple[AgentKey, ...] = (
    "SHOPPING",
    "SUPPORT",
    "OUTBOUND_SALES",
    "WHOLESALE",
)

#: Template revision. Bump when a prompt or the graph changes, so an install
#: can tell the store which agents have a newer starter version available.
TEMPLATE_REVISION = 1

WORKFLOW_NAMES: dict[AgentKey, str] = {
    "SHOPPING": "Holiday Voices - Shopping & Phone Orders",
    "SUPPORT": "Holiday Voices - Customer Support",
    "OUTBOUND_SALES": "Holiday Voices - Outbound Sales",
    "WHOLESALE": "Holiday Voices - Wholesale",
}

INBOUND: dict[AgentKey, bool] = {
    "SHOPPING": True,
    "SUPPORT": True,
    "OUTBOUND_SALES": False,
    "WHOLESALE": True,
}

STORE = "{{hv_store_name | Holiday Voices}}"

GLOBAL_PROMPT = f"""# {STORE} — RULES FOR EVERY STEP

## Who you are
You are {STORE}'s automated phone assistant. {STORE} sells recordable keepsake
Christmas ornaments: the customer records a short message of their own voice
into the ornament, and it plays back when pressed. You are not a person and
never claim or imply to be one. If asked, say you're the store's automated
assistant and you can connect them with the team.

## How you speak
- Warm, unhurried, brief. One or two short sentences per turn, then let them talk.
- Ask one question at a time. Never read lists longer than three items.
- Say prices as people say them ("thirty dollars"), never "USD".
- Never say a tool name, "session", "system" or "database".

## What only the tools may tell you
- Prices, finishes, what is in stock, shipping, tax, order status and tracking
  come ONLY from the tools. Never guess, round, or remember them from earlier calls.
- Never invent a discount, a delivery date, a review, a feature or urgency
  ("only a few left"). If you don't know, say the team will follow up.
- Every tool answer has an `instruction`: follow it.

## Money and privacy
- Never take card numbers or payment by phone. Payment always happens on the
  secure checkout page the store sends.
- A checkout link sent is NOT a sale: never say the order is placed, paid or
  confirmed. Say they can finish it from the link whenever they're ready.
- Order status and tracking are only given when the lookup tool says the caller
  is verified. Otherwise say you can't share it on this call and offer a person.

## Being kind
Many ornaments preserve the voice of someone loved, sometimes someone who has
died. If a caller mentions loss, acknowledge it simply and gently. Never use it
to sell, and never pressure.

## Stops
- "Don't call me", "take me off your list": confirm warmly and end_call
  (reason do_not_call). Never argue.
- Voicemail, an answering machine or a call screener: say nothing, end_call
  (reason voicemail).
- Silence after two tries: end_call (reason no_response).
- A person asked for: request_human_transfer with a short summary, then
  transfer_to_team. If the transfer is declined, offer schedule_callback.
"""

PRODUCT_FACTS = """## About the ornaments (confirm specifics with the tools)
- Each ornament records one short message (get_product_details gives the exact
  length) and plays it when pressed. Customers record at home after it arrives.
- Practising the message first helps; the store's website has a free Memory
  Studio to write and rehearse it. A practice recording on the website is NOT
  transferred to the ornament: they record into the ornament itself.
"""

DISPOSITION_TEXT = ", ".join(DISPOSITIONS)


def _node(
    node_id: str, type_: str, x: float, y: float, data: dict[str, Any]
) -> dict[str, Any]:
    return {"id": node_id, "type": type_, "position": {"x": x, "y": y}, "data": data}


def _edge(
    source: str, target: str, label: str, condition: str, **extra: Any
) -> dict[str, Any]:
    return {
        "id": f"e-{source}-{target}",
        "source": source,
        "target": target,
        "data": {"label": label, "condition": condition, **extra},
    }


def _agent(
    node_id: str,
    name: str,
    x: float,
    y: float,
    prompt: str,
    tools: list[str],
) -> dict[str, Any]:
    return _node(
        node_id,
        "agentNode",
        x,
        y,
        {
            "name": name,
            "prompt": prompt,
            "allow_interrupt": True,
            "add_global_prompt": True,
            "extraction_enabled": False,
            "tool_uuids": tools,
        },
    )


#: Facts every call reports (extracted at wrap-up; never shown to the caller).
_COMMON_EXTRACTION = [
    {
        "name": "call_intent",
        "type": "string",
        "prompt": "One of: shopping, order_status, product_question, recording_help, "
        "return_or_damage, wholesale, callback, other.",
    },
    {
        "name": "units_discussed",
        "type": "number",
        "prompt": "How many ornaments the caller wanted, if they said. Blank otherwise.",
    },
    {
        "name": "finishes_discussed",
        "type": "string",
        "prompt": "Ornament finishes the caller chose, e.g. '2 red, 1 gold'. Blank if none.",
    },
    {
        "name": "caller_frustrated",
        "type": "boolean",
        "prompt": "True only if the caller clearly expressed frustration or annoyance.",
    },
    {
        "name": "resolved_on_call",
        "type": "boolean",
        "prompt": "True if what the caller called about was fully handled on this call.",
    },
]

_WHOLESALE_EXTRACTION = [
    {"name": "business_name", "type": "string", "prompt": "The business's name."},
    {
        "name": "business_type",
        "type": "string",
        "prompt": "e.g. gift shop, hospice, church, florist, corporate gifting.",
    },
    {
        "name": "wholesale_quantity",
        "type": "number",
        "prompt": "Approximate number of ornaments they asked about.",
    },
    {
        "name": "needed_by",
        "type": "string",
        "prompt": "When they need them, in their words.",
    },
    {
        "name": "contact_email",
        "type": "string",
        "prompt": "Email they gave for the quote.",
    },
]


def _end(tools: list[str], extraction: list[dict[str, Any]]) -> dict[str, Any]:
    return _node(
        "end",
        "endCall",
        900,
        620,
        {
            "name": "Wrap Up",
            "prompt": (
                "# WRAP UP\n\n"
                "One short, warm closing line that fits what happened (for example: "
                "'Thanks so much for calling - happy holidays!'). Do not list what was "
                "recorded. Then call end_call with the reason: "
                + DISPOSITION_TEXT
                + "."
            ),
            "allow_interrupt": True,
            "add_global_prompt": True,
            "extraction_enabled": True,
            "extraction_prompt": "Extract only facts the caller actually stated.",
            "extraction_variables": extraction,
            "tool_uuids": tools,
            "is_end": True,
        },
    )


def _reporting(base_url: str, credential_uuid: str, agent: AgentKey) -> dict[str, Any]:
    return _node(
        "holiday_voices",
        "holiday_voices",
        -460,
        -300,
        {
            "name": "Holiday Voices",
            "holiday_voices_enabled": True,
            "holiday_voices_base_url": base_url.rstrip("/"),
            "holiday_voices_credential_uuid": credential_uuid,
            "holiday_voices_agent": agent,
            "holiday_voices_template_revision": TEMPLATE_REVISION,
        },
    )


def _start(
    agent: AgentKey,
    greeting: str,
    prompt: str,
    tools: list[str],
    *,
    base_url: str,
    credential_uuid: str,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Greeting",
        "greeting_type": "text",
        "greeting": greeting,
        "prompt": prompt,
        "allow_interrupt": True,
        "add_global_prompt": True,
        "delayed_start": True,
        "delayed_start_duration": 0.5,
        "extraction_enabled": False,
        "tool_uuids": tools,
        "is_start": True,
    }
    if INBOUND[agent]:
        data.update(
            {
                "pre_call_fetch_enabled": True,
                "pre_call_fetch_url": pre_call_fetch_url(base_url),
                "pre_call_fetch_credential_uuid": credential_uuid,
            }
        )
    return _node("greeting", "startCall", -160, 40, data)


def build_workflow(
    agent: AgentKey,
    tools: dict[str, str],
    *,
    base_url: str,
    credential_uuid: str,
) -> dict[str, Any]:
    """A workflow_definition for ``agent`` wired to the installed tool UUIDs."""

    def refs(*names: str) -> list[str]:
        return [tools[name] for name in names if name in tools]

    people = ("request_human_transfer", "transfer_to_team", "schedule_callback")
    catalog = ("get_products", "get_product_details", "check_inventory")
    order = (
        "add_order_item",
        "update_order_item",
        "remove_order_item",
        "get_order_summary",
        "calculate_shipping",
        "calculate_taxes",
        "apply_valid_promotion",
    )
    checkout = ("create_customer", "create_secure_checkout", "send_checkout_link")
    status = ("lookup_order", "get_tracking")

    global_node = _node(
        "global",
        "globalNode",
        -40,
        -300,
        {
            "name": "Store Rules",
            "prompt": GLOBAL_PROMPT + "\n" + PRODUCT_FACTS,
            "allow_interrupt": True,
            "add_global_prompt": False,
        },
    )

    if agent == "SHOPPING":
        start = _start(
            agent,
            "Thanks for calling {{hv_store_name | Holiday Voices}}! I'm the automated "
            "assistant. I can help you choose an ornament, place an order, or check on an "
            "existing one. What can I help you with?",
            "# GREETING\n\n"
            "If {{hv_caller_known | false}} is true you may greet them by first name "
            "({{hv_customer_first_name | unknown}}) once they speak. Find out what they need in one "
            "question. Shopping or ordering: move on. An existing order, a problem, or a "
            "recording question: move to Help. A business or bulk (wholesale) inquiry: "
            "create_support_request with topic 'Wholesale inquiry' and their details, then "
            "offer a person.",
            refs(
                "lookup_customer",
                "get_products",
                "create_support_request",
                *people,
                "end_call",
            ),
            base_url=base_url,
            credential_uuid=credential_uuid,
        )
        nodes = [
            global_node,
            start,
            _agent(
                "shop",
                "Help Them Choose",
                380,
                -120,
                "# HELP THEM CHOOSE\n\n"
                "Ask who the ornament is for. Call get_products before naming finishes or "
                "prices. Recommend simply (one per grandchild, a matching set for a family). "
                "Confirm each finish and quantity out loud, then add_order_item. Corrections "
                "('make the gold ones red') use update_order_item / remove_order_item. When "
                "they're happy, get_order_summary and read it back briefly.",
                refs(*catalog, *order, "create_order_draft", *people),
            ),
            _agent(
                "checkout",
                "Secure Checkout",
                860,
                -120,
                "# SECURE CHECKOUT\n\n"
                "Shipping and tax: ask the delivery state and ZIP, then calculate_shipping "
                "(and calculate_taxes if they ask). Get their first and last name and email; "
                "spell the email back, then create_customer. Then create_secure_checkout and "
                "send_checkout_link. Tell them the link is on its way and that they pay on "
                "the store's secure page. Never say the order is placed.",
                refs(*order, *checkout, *people),
            ),
            _agent(
                "help",
                "Order Help",
                380,
                260,
                "# ORDER HELP\n\n"
                "For an existing order ask the order number (and the email on it if they're "
                "calling from another phone), then lookup_order or get_tracking. Product or "
                "recording questions: get_product_details. Anything you can't resolve: "
                "create_support_request with a clear summary.",
                refs(*status, "get_product_details", "create_support_request", *people),
            ),
            _end(refs("end_call"), _COMMON_EXTRACTION),
        ]
        edges = [
            _edge(
                "greeting",
                "shop",
                "wants to shop",
                "The caller wants to buy, order, or hear about ornaments.",
            ),
            _edge(
                "greeting",
                "help",
                "needs help",
                "The caller is asking about an existing order, a problem, or how to record.",
            ),
            _edge(
                "greeting",
                "end",
                "cannot continue",
                "Wrong number, voicemail, do-not-call, a transfer happened, or no response.",
            ),
            _edge(
                "shop",
                "checkout",
                "ready to check out",
                "The caller confirmed what they want and is ready to order or wants the link.",
            ),
            _edge(
                "shop",
                "help",
                "needs help instead",
                "The caller switches to an existing order or a product problem.",
            ),
            _edge(
                "shop",
                "end",
                "done",
                "The caller is finished without ordering, was transferred, or asked for a callback.",
            ),
            _edge(
                "checkout",
                "shop",
                "change the order",
                "The caller wants to change items before checkout.",
            ),
            _edge(
                "checkout",
                "end",
                "link sent or done",
                "The checkout link was sent, or the caller is finished.",
            ),
            _edge(
                "help",
                "shop",
                "wants to shop",
                "After help, the caller wants to buy more ornaments.",
            ),
            _edge(
                "help",
                "end",
                "help done",
                "The question was answered, a request was created, or they were transferred.",
            ),
        ]
    elif agent == "SUPPORT":
        start = _start(
            agent,
            "Thanks for calling {{hv_store_name | Holiday Voices}}! I'm the automated "
            "assistant. I can check on an order, help with recording your ornament, or get "
            "you to the team. What's going on?",
            "# GREETING\n\nListen for the problem in one question. Order status or "
            "tracking: move to Order Status. Recording, battery, or product questions, a "
            "damaged or wrong item, a return question: move to Troubleshoot.",
            refs("lookup_customer", *people, "end_call"),
            base_url=base_url,
            credential_uuid=credential_uuid,
        )
        nodes = [
            global_node,
            start,
            _agent(
                "status",
                "Order Status",
                380,
                -120,
                "# ORDER STATUS\n\nAsk the order number. If they're not calling from the "
                "phone on the order, ask the email on it. Use lookup_order, then get_tracking "
                "if it shipped. If the tool says it cannot be verified, share nothing about "
                "the order and offer to open a request for the team.",
                refs(*status, "create_support_request", *people),
            ),
            _agent(
                "troubleshoot",
                "Troubleshoot",
                380,
                260,
                "# TROUBLESHOOT\n\nUse get_product_details for how-to answers (recording, "
                "playback, battery). Walk them through one step at a time and check it "
                "worked. Damaged, defective or wrong item, or a return: never promise a "
                "refund or replacement; create_support_request with the order number and a "
                "two-sentence summary, and tell them the team will follow up. Frustrated or "
                "asking for a person: request_human_transfer then transfer_to_team.",
                refs("get_product_details", *status, "create_support_request", *people),
            ),
            _end(refs("end_call"), _COMMON_EXTRACTION),
        ]
        edges = [
            _edge(
                "greeting",
                "status",
                "order status",
                "The caller asks where an order is, or for tracking.",
            ),
            _edge(
                "greeting",
                "troubleshoot",
                "product help",
                "A recording, product, damage, return or other problem.",
            ),
            _edge(
                "greeting",
                "end",
                "cannot continue",
                "Wrong number, voicemail, do-not-call, transferred, or no response.",
            ),
            _edge(
                "status",
                "troubleshoot",
                "has a problem",
                "The order arrived and something is wrong with it.",
            ),
            _edge(
                "status",
                "end",
                "answered",
                "Status given, request created, transferred, or the caller is finished.",
            ),
            _edge(
                "troubleshoot",
                "status",
                "order question",
                "The caller now asks about an order's status.",
            ),
            _edge(
                "troubleshoot",
                "end",
                "resolved or handed off",
                "Fixed, request created, transferred, callback booked, or finished.",
            ),
        ]
    elif agent == "OUTBOUND_SALES":
        start = _start(
            agent,
            "Hi, this is the automated assistant from {{hv_store_name | Holiday Voices}}. "
            "I'm calling because {{hv_call_reason | you asked "
            "us to call you back}}. Is now a good time?",
            "# OPENING (outbound)\n\n"
            "This call was placed because: {{hv_call_reason | they asked for a callback}}"
            " ({{hv_campaign_name | callback}}). Never say they asked for anything they did "
            "not. If they're busy, offer schedule_callback. If they say not interested, "
            "thank them and end_call (not_interested). If they ask not to be called, end_call "
            "(do_not_call) after confirming. Wrong person: apologise, share nothing, end_call "
            "(wrong_number).",
            refs("lookup_customer", "schedule_callback", *people, "end_call"),
            base_url=base_url,
            credential_uuid=credential_uuid,
        )
        nodes = [
            global_node,
            start,
            _agent(
                "offer",
                "Help Them Order",
                380,
                -120,
                "# HELP THEM ORDER\n\nPick up where they left off: if they had items in a "
                "bag or asked a question, address that first. Use get_products for finishes "
                "and prices. Build the order with add_order_item and confirm it with "
                "get_order_summary. Keep it short and never pressure; one gentle offer only.",
                refs(*catalog, *order, "create_order_draft", *people),
            ),
            _agent(
                "checkout",
                "Secure Checkout",
                860,
                -120,
                "# SECURE CHECKOUT\n\nConfirm delivery state and ZIP for calculate_shipping, "
                "confirm their email (spell it back) and create_customer, then "
                "create_secure_checkout and send_checkout_link. A sent link is not a sale.",
                refs(*order, *checkout, *people),
            ),
            _end(refs("end_call"), _COMMON_EXTRACTION),
        ]
        edges = [
            _edge(
                "greeting",
                "offer",
                "good time",
                "The person confirmed who they are and has a moment to talk.",
            ),
            _edge(
                "greeting",
                "end",
                "not now",
                "Busy (callback booked), not interested, do-not-call, wrong person, voicemail, or no response.",
            ),
            _edge(
                "offer",
                "checkout",
                "ready",
                "They confirmed items and want the checkout link.",
            ),
            _edge(
                "offer",
                "end",
                "done",
                "They decline, ask for a callback, were transferred, or are finished.",
            ),
            _edge("checkout", "offer", "change", "They want to change items first."),
            _edge(
                "checkout",
                "end",
                "link sent",
                "The link was sent, or they are finished.",
            ),
        ]
    else:  # WHOLESALE
        start = _start(
            agent,
            "Thanks for calling {{hv_store_name | Holiday Voices}}! I'm the automated "
            "assistant. Are you calling about ornaments for a business or organization?",
            "# GREETING\n\nConfirm it is a business inquiry. A personal order: say you can "
            "help with that too and move on to Qualify only for business needs; for a "
            "personal order, create_support_request with topic 'Personal order by phone' "
            "and offer transfer_to_team.",
            refs("lookup_customer", "create_support_request", *people, "end_call"),
            base_url=base_url,
            credential_uuid=credential_uuid,
        )
        nodes = [
            global_node,
            start,
            _agent(
                "qualify",
                "Qualify",
                380,
                -120,
                "# QUALIFY THE INQUIRY\n\nLearn, one question at a time: the business name "
                "and type, roughly how many ornaments, which finishes, and when they need "
                "them, plus a name and email for the quote. Product questions: "
                "get_product_details; finishes and retail prices: get_products. NEVER quote a "
                "wholesale price, discount, or terms: a person prepares every quote. Then "
                "create_support_request with topic 'Wholesale inquiry' and all of it in "
                "details, and create_customer with their contact.",
                refs(*catalog, "create_customer", "create_support_request", *people),
            ),
            _agent(
                "handoff",
                "Hand to a Person",
                860,
                -120,
                "# HAND TO A PERSON\n\nOffer to connect them with the team now "
                "(request_human_transfer, then transfer_to_team). If nobody is free or they "
                "prefer, schedule_callback. Tell them the team will send a quote; never "
                "promise a price or a date.",
                refs(*people, "create_support_request"),
            ),
            _end(refs("end_call"), _COMMON_EXTRACTION + _WHOLESALE_EXTRACTION),
        ]
        edges = [
            _edge(
                "greeting",
                "qualify",
                "business inquiry",
                "The caller confirms a business, organization or bulk inquiry.",
            ),
            _edge(
                "greeting",
                "end",
                "cannot continue",
                "Wrong number, voicemail, do-not-call, transferred, or no response.",
            ),
            _edge(
                "qualify",
                "handoff",
                "qualified",
                "The inquiry is recorded with enough detail for a quote.",
            ),
            _edge(
                "qualify",
                "end",
                "not a fit",
                "They are only browsing, decline to share details, or are finished.",
            ),
            _edge(
                "handoff",
                "end",
                "handed off",
                "Transferred, callback booked, or they are finished.",
            ),
        ]

    nodes.append(_reporting(base_url, credential_uuid, agent))
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.75}}
