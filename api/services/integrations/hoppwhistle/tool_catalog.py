"""The Hoppwhistle tools, as ordinary Dograh tool definitions.

Hoppwhistle (the insurance CRM, telephony and routing system) owns the
customer, consent, licensing and routing. Dograh owns the conversation. The
two meet only through these tools: each is a plain ``http_api`` tool (or, for
the transfer, a ``transfer_call`` tool with a dynamic resolver), so they run on
Dograh's existing tool machinery -- no new runtime path, and nothing in Dograh
reads Hoppwhistle's database.

Every tool sends two preset parameters the LLM never sees and cannot override
(``{**llm_args, **presets}`` in ``execute_http_tool``):

    session_id     {{initial_context.hw_session_id}}
    session_token  {{initial_context.hw_session_token}}

Hoppwhistle puts both in the call's initial context when it starts the call
(or answers the pre-call fetch for an inbound one), and the session decides
the agency. The service credential is a bearer credential on every tool.

The contract is documented on both sides:
    Hoppwhistle  docs/ai-agents/DOGRAH_CONTRACT.md, apps/api/src/services/ai-agent/contract.ts
    Dograh       docs/integrations/hoppwhistle.mdx
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "2026-10-31"

#: Tool names are the function names the LLM sees (``custom_tool_function_name``
#: leaves lower_snake_case untouched) and the names the workflow prompts use.
TOOL_NAMES = (
    "lookup_customer",
    "update_customer",
    "save_qualification",
    "eligible_agents",
    "create_callback",
    "mark_dnc",
    "transfer_to_agent",
    "end_call",
)

SESSION_PRESETS: list[dict[str, Any]] = [
    {
        "name": "session_id",
        "type": "string",
        "value_template": "{{initial_context.hw_session_id}}",
        "required": True,
    },
    {
        "name": "session_token",
        "type": "string",
        "value_template": "{{initial_context.hw_session_token}}",
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


#: name -> (description shown to the LLM, LLM parameters)
_HTTP_TOOLS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "lookup_customer": (
        "Call once, right after the person answers, before discussing anything. Returns the "
        "first name to confirm identity, the state and age on file, whether they are an "
        "existing customer, why we are calling, and whether the number is do-not-call. "
        "Follow the returned `instruction`.",
        [],
    ),
    "update_customer": (
        "Save a correction the person gives you: their first or last name, state, age or "
        "preferred language. Set wrong_person=true the moment you learn you are not "
        "speaking with the prospect; then share nothing about them. Follow the returned "
        "`instruction`.",
        [
            _param("first_name", "string", "First name, as the person said it."),
            _param("last_name", "string", "Last name, as the person said it."),
            _param(
                "state", "string", "US state they live in (name or two-letter code)."
            ),
            _param("age", "number", "Age in years."),
            _param(
                "preferred_language",
                "string",
                "Language they prefer, e.g. 'es' for Spanish.",
            ),
            _param(
                "wrong_person",
                "boolean",
                "True when you are not speaking with the prospect.",
            ),
        ],
    ),
    "save_qualification": (
        "Save each qualification answer as soon as you hear it. The response says the ONE "
        "next question to ask (ask exactly that, nothing more), or that they qualified "
        "(offer the licensed agent immediately), or that they do not qualify (wrap up "
        "politely). Never ask for social security, banking, card or health details.",
        [
            _param(
                "identity_confirmed", "boolean", "They confirmed they are the prospect."
            ),
            _param(
                "interested",
                "boolean",
                "They are interested in final expense coverage.",
            ),
            _param("state", "string", "US state they live in."),
            _param("age", "number", "Their exact age, when they gave it."),
            _param(
                "age_range", "string", "An age range they gave instead, e.g. '60-69'."
            ),
            _param(
                "independent_living",
                "boolean",
                "They live at home, not in a care facility.",
            ),
            _param(
                "language", "string", "The language of the conversation, e.g. 'es'."
            ),
            _param(
                "extra_answers",
                "object",
                "Answers to the agency's own questions, keyed by the question key the "
                "previous response named.",
            ),
        ],
    ),
    "eligible_agents": (
        "Check whether a licensed agent who can take this prospect's state is free right "
        "now, before promising a transfer. Never mention agent names.",
        [],
    ),
    "create_callback": (
        "Book a callback from a licensed agent when a live transfer is not possible or the "
        "person prefers a later time. Give callback_at as ISO 8601 with an offset when they "
        "name a time; omit it for 'as soon as possible'. Tell them the time the response "
        "gives you, which may be moved into allowed calling hours.",
        [
            _param(
                "callback_at",
                "string",
                "ISO 8601 time with offset, e.g. 2026-11-02T16:00:00-06:00.",
            ),
            _param(
                "callback_time_text",
                "string",
                "What they said, e.g. 'tomorrow after lunch'.",
            ),
            _param(
                "notes", "string", "Anything the agent should know, in one sentence."
            ),
        ],
    ),
    "mark_dnc": (
        "Call IMMEDIATELY when the person asks not to be called again, to be removed from "
        "the list, or says do not call. Then confirm and end the call; do not try to change "
        "their mind.",
        [_param("reason", "string", "Their words, briefly.")],
    ),
}


def _http_tool(base_url: str, credential_uuid: str | None, name: str) -> dict[str, Any]:
    description, parameters = _HTTP_TOOLS[name]
    return {
        "name": name,
        "description": description,
        "category": "http_api",
        "icon": "phone",
        "icon_color": "#0F766E",
        "definition": {
            "schema_version": 1,
            "type": "http_api",
            "config": {
                "method": "POST",
                "url": f"{base_url}/api/internal/ai-agent/tools/{name}",
                "credential_uuid": credential_uuid,
                "parameters": parameters or None,
                "preset_parameters": SESSION_PRESETS,
                "timeout_ms": 6000,
            },
        },
    }


def _transfer_tool(base_url: str, credential_uuid: str | None) -> dict[str, Any]:
    return {
        "name": "transfer_to_agent",
        "description": (
            "Transfer the prospect to a licensed agent. Only call after they qualified and "
            "agreed to speak with an agent, or after they asked for a person. Hoppwhistle "
            "picks an agent licensed in their state who is free; if nobody is, the transfer "
            "is declined with what to say -- offer a callback."
        ),
        "category": "transfer_call",
        "icon": "phone-forwarded",
        "icon_color": "#0F766E",
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
                    "url": f"{base_url}/api/internal/ai-agent/transfer",
                    "credential_uuid": credential_uuid,
                    "timeout_ms": 4000,
                    "parameters": [
                        _param(
                            "reason",
                            "string",
                            "One sentence for the agent: what they want.",
                        ),
                        _param(
                            "prospect_requested_agent",
                            "boolean",
                            "True when the prospect asked for a person before qualifying.",
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
            "End the call after your closing line. Give the reason: transferred, "
            "callback_scheduled, not_interested, not_qualified, dnc_requested, wrong_person, "
            "voicemail, existing_customer or no_response."
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
                "endCallReasonDescription": (
                    "One of: transferred, callback_scheduled, not_interested, not_qualified, "
                    "dnc_requested, wrong_person, voicemail, existing_customer, no_response."
                ),
            },
        },
    }


def build_tool_definitions(
    base_url: str, credential_uuid: str | None
) -> list[dict[str, Any]]:
    """Every Hoppwhistle tool, ready for ``CreateToolRequest``."""
    base = base_url.rstrip("/")
    return [
        *(_http_tool(base, credential_uuid, name) for name in _HTTP_TOOLS),
        _transfer_tool(base, credential_uuid),
        _end_call_tool(),
    ]


def pre_call_fetch_url(base_url: str) -> str:
    """Where an inbound (after-hours) workflow's start node fetches its session."""
    return f"{base_url.rstrip('/')}/api/internal/ai-agent/inbound"
