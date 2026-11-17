"""The Holiday Voices commerce tools, as ordinary Dograh tool definitions.

Holiday Voices is a retail store (recordable keepsake ornaments) whose
catalog, customers, orders, checkout and support live in Hoppwhistle's retail
workspace; WooCommerce stays the authority for price, stock, tax and payment.
Dograh holds the conversation. The two meet only through these tools, all
plain ``http_api`` tools (plus one ``transfer_call`` with a dynamic resolver
and one ``end_call``), so they run on Dograh's existing tool machinery.

Every HTTP tool sends two preset parameters the LLM never sees and cannot
override (``{**llm_args, **presets}`` in ``execute_http_tool``):

    session_id     {{initial_context.hv_session_id}}
    session_token  {{initial_context.hv_session_token}}

Hoppwhistle mints both, inside the store's tenant, when it answers the
pre-call fetch (inbound) or starts the call (outbound). The bearer credential
is the store's own engine key (``hvk_...``): Hoppwhistle derives the tenant
from it, and refuses the insurance engine secret on these routes.

The contract is documented on both sides:
    Hoppwhistle  docs/holiday-voices/AI_VOICE_SPECIFICATION.md,
                 apps/api/src/services/commerce/voice-tools.ts
    Dograh       docs/integrations/holiday-voices.mdx
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "2026-11-17"

#: Path prefix of every Holiday Voices engine route in Hoppwhistle.
ENGINE_PREFIX = "/api/internal/hv/voice"

SESSION_PRESETS: list[dict[str, Any]] = [
    {
        "name": "session_id",
        "type": "string",
        "value_template": "{{initial_context.hv_session_id}}",
        "required": True,
    },
    {
        "name": "session_token",
        "type": "string",
        "value_template": "{{initial_context.hv_session_token}}",
        "required": True,
    },
]


def _param(
    name: str, type_: str, description: str, required: bool = False
) -> dict[str, Any]:
    return {
        "name": name,
        "type": type_,
        "description": description,
        "required": required,
    }


_COLOR = _param(
    "color", "string", "The ornament finish the caller named, e.g. red, green or gold."
)
_SKU = _param("sku", "string", "The SKU, only when a tool gave you one.")
_STATE = _param("state", "string", "Delivery state (name or two-letter code).")
_ZIP = _param("zip", "string", "5-digit delivery ZIP code.")
_ORDER_NUMBER = _param(
    "order_number", "number", "The order number the caller read out.", required=True
)
_ORDER_EMAIL = _param(
    "email",
    "string",
    "The email on the order, when the caller is not calling from the order's phone.",
)

#: tool name (what the LLM sees) -> (Hoppwhistle tool, description, parameters).
#: The Hoppwhistle tool names are the route segment; they are fixed by
#: VOICE_TOOL_NAMES in voice-tools.ts and must not drift.
HTTP_TOOLS: dict[str, tuple[str, str, list[dict[str, Any]]]] = {
    "get_products": (
        "getProducts",
        "List the ornaments for sale with their real prices and what each one is. Call "
        "before describing products or prices. Never quote a price you did not get here.",
        [],
    ),
    "get_product_details": (
        "getProductDetails",
        "Answer a specific product question (battery, recording length, re-recording, "
        "size, care) from the store's own written answers. If it says it is not certain, "
        "say so and offer to have the team follow up.",
        [
            _param(
                "product",
                "string",
                "The product and question, e.g. 'gold ornament battery'.",
                required=True,
            )
        ],
    ),
    "check_inventory": (
        "checkInventory",
        "Whether a finish is available in the quantity asked. Answers yes or no only: never "
        "say how many are left and never create urgency.",
        [_COLOR, _SKU, _param("quantity", "number", "How many they want.")],
    ),
    "lookup_customer": (
        "lookupCustomer",
        "Whether the caller is a known customer, by caller ID. Gives a first name only. "
        "Call once near the start when it would help; never read back details.",
        [],
    ),
    "create_customer": (
        "createCustomer",
        "Save the caller's name and email (and a phone if different from caller ID) so "
        "the order and checkout link reach them. Spell back the email before calling.",
        [
            _param("first_name", "string", "First name."),
            _param("last_name", "string", "Last name."),
            _param("email", "string", "Email address, confirmed letter by letter."),
            _param("phone", "string", "A phone number other than the one calling."),
        ],
    ),
    "create_order_draft": (
        "createOrderDraft",
        "Start this call's order. Optional: adding an item starts one automatically.",
        [],
    ),
    "add_order_item": (
        "addOrderItem",
        "Add ornaments to this call's order. Price comes from the catalog. Confirm the "
        "finish and quantity with the caller first.",
        [
            _COLOR,
            _SKU,
            _param("quantity", "number", "How many to add.", required=True),
        ],
    ),
    "update_order_item": (
        "updateOrderItem",
        "Change how many of a finish are on this call's order (0 removes it).",
        [
            _COLOR,
            _SKU,
            _param(
                "quantity", "number", "The new total for that finish.", required=True
            ),
        ],
    ),
    "remove_order_item": (
        "removeOrderItem",
        "Remove a finish from this call's order.",
        [_COLOR, _SKU],
    ),
    "get_order_summary": (
        "getOrderSummary",
        "Read back what is on this call's order and the subtotal. Use before checkout and "
        "whenever the caller asks what they have.",
        [],
    ),
    "calculate_shipping": (
        "calculateShipping",
        "The store's own shipping quote for this order. Needs the delivery state and ZIP. "
        "Never give a delivery date.",
        [_STATE, _ZIP],
    ),
    "calculate_taxes": (
        "calculateTaxes",
        "The store's own tax quote for this order. Needs the delivery state and ZIP.",
        [_STATE, _ZIP],
    ),
    "apply_valid_promotion": (
        "applyValidPromotion",
        "Check a promotion code the caller gives you. Never invent or suggest a discount.",
        [_param("code", "string", "The code exactly as the caller said it.")],
    ),
    "create_secure_checkout": (
        "createSecureCheckout",
        "Create the secure checkout link for this order once the caller confirmed the "
        "items. Payment always happens on that page: never take card details by phone.",
        [],
    ),
    "send_checkout_link": (
        "sendCheckoutLink",
        "Send the checkout link by email (or text, when the store can text). A sent link "
        "is not a sale: never say the order is placed or paid.",
        [
            _param("channel", "string", "email or sms."),
            _param("email", "string", "Where to email it, when not already saved."),
        ],
    ),
    "lookup_order": (
        "lookupOrder",
        "Status of an existing order. Only answered when the caller is verified by the "
        "order's phone or email; otherwise nothing is disclosed.",
        [_ORDER_NUMBER, _ORDER_EMAIL],
    ),
    "get_tracking": (
        "getTracking",
        "Carrier and tracking number of a shipped order, verified the same way.",
        [_ORDER_NUMBER, _ORDER_EMAIL],
    ),
    "create_support_request": (
        "createSupportRequest",
        "Open a support request for the team (broken ornament, recording trouble, wrong "
        "item, return question, wholesale inquiry). Never promise a refund or a time.",
        [
            _param(
                "topic", "string", "A short title, e.g. 'Ornament will not play'.", True
            ),
            _param("details", "string", "What happened, in two or three sentences."),
            _param("order_number", "number", "Their order number, if they have one."),
        ],
    ),
    "request_human_transfer": (
        "requestHumanTransfer",
        "Record that the caller wants a person, with a summary the team will read. Then "
        "use transfer_to_team, or schedule_callback when nobody can take the call.",
        [
            _param("reason", "string", "Why, in one sentence.", required=True),
            _param("summary", "string", "What the team needs to know, briefly."),
        ],
    ),
    "schedule_callback": (
        "scheduleCallback",
        "Ask the team to call the caller back. Confirm the time in the caller's words; "
        "never promise an exact time.",
        [
            _param(
                "preferred_time",
                "string",
                "When they would like the call, in their words.",
                required=True,
            ),
            _param("reason", "string", "What the callback is about."),
        ],
    ),
}

TOOL_NAMES: tuple[str, ...] = (*HTTP_TOOLS, "transfer_to_team", "end_call")

#: Dispositions every Holiday Voices workflow reports through end_call.
DISPOSITIONS: tuple[str, ...] = (
    "checkout_link_sent",
    "order_help_given",
    "order_status_given",
    "support_request_created",
    "transferred",
    "callback_scheduled",
    "wholesale_lead",
    "information_only",
    "not_interested",
    "do_not_call",
    "wrong_number",
    "voicemail",
    "no_response",
)

ICON_COLOR = "#7A1F2B"  # Holiday Voices garnet


def _http_tool(base_url: str, credential_uuid: str | None, name: str) -> dict[str, Any]:
    route, description, parameters = HTTP_TOOLS[name]
    return {
        "name": name,
        "description": description,
        "category": "http_api",
        "icon": "gift",
        "icon_color": ICON_COLOR,
        "definition": {
            "schema_version": 1,
            "type": "http_api",
            "config": {
                "method": "POST",
                "url": f"{base_url}{ENGINE_PREFIX}/tools/{route}",
                "credential_uuid": credential_uuid,
                "parameters": parameters or None,
                "preset_parameters": SESSION_PRESETS,
                "timeout_ms": 8000,
            },
        },
    }


def _transfer_tool(base_url: str, credential_uuid: str | None) -> dict[str, Any]:
    return {
        "name": "transfer_to_team",
        "description": (
            "Connect the caller to a person on the Holiday Voices team. Only after they "
            "asked for a person or the issue needs one, and after request_human_transfer. "
            "Hoppwhistle picks the store's own team line; if nobody can take the call the "
            "transfer is declined with what to say -- offer a callback."
        ),
        "category": "transfer_call",
        "icon": "phone-forwarded",
        "icon_color": ICON_COLOR,
        "definition": {
            "schema_version": 1,
            "type": "transfer_call",
            "config": {
                "destination_source": "dynamic",
                "destination": "",
                "messageType": "none",
                "timeout": 30,
                "resolver": {
                    "type": "http",
                    "url": f"{base_url}{ENGINE_PREFIX}/transfer",
                    "credential_uuid": credential_uuid,
                    "timeout_ms": 4000,
                    "parameters": [
                        _param(
                            "reason",
                            "string",
                            "One sentence for the team member: what the caller needs.",
                        ),
                    ],
                    "preset_parameters": SESSION_PRESETS,
                },
            },
        },
    }


def _end_call_tool() -> dict[str, Any]:
    return {
        "name": "end_call",
        "description": (
            "End the call after your closing line, with the reason: "
            + ", ".join(DISPOSITIONS)
            + "."
        ),
        "category": "end_call",
        "icon": "phone-off",
        "icon_color": "#B91C1C",
        "definition": {
            "schema_version": 1,
            "type": "end_call",
            "config": {
                "messageType": "none",
                "endCallReason": True,
                "endCallReasonDescription": "One of: " + ", ".join(DISPOSITIONS) + ".",
            },
        },
    }


def build_tool_definitions(
    base_url: str, credential_uuid: str | None
) -> list[dict[str, Any]]:
    """Every Holiday Voices tool, ready for ``CreateToolRequest``."""
    base = base_url.rstrip("/")
    return [
        *(_http_tool(base, credential_uuid, name) for name in HTTP_TOOLS),
        _transfer_tool(base, credential_uuid),
        _end_call_tool(),
    ]


def pre_call_fetch_url(base_url: str) -> str:
    """Where an inbound workflow's start node opens its Hoppwhistle session."""
    return f"{base_url.rstrip('/')}{ENGINE_PREFIX}/inbound"


def completion_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{ENGINE_PREFIX}/runs/complete"
