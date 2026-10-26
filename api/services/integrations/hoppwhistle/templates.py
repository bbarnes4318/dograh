"""Starter workflows for the two Hoppwhistle AI products.

Built from Dograh's own pieces: a global node for the compliance rules every
node shares, a start node, agent nodes, an end node, LLM transitions, and the
Hoppwhistle tools by UUID. An agency's operator edits them in the Dograh
editor like any other workflow; the rules that keep the AI compliant also live
in Hoppwhistle's tool responses (an ``instruction`` on every answer), so a
prompt edit cannot quietly drop them.

    AI Callback Recovery     outbound: Hoppwhistle starts the call
    AI Callback Recovery     inbound (after hours): the start node fetches
                             the session from Hoppwhistle before speaking
    AI Lead Qualifier        outbound: Hoppwhistle starts the call

Prompts read only what Hoppwhistle put in ``initial_context``:
``persona_name``, ``agency_name``, ``first_name``, ``reason_for_call``,
``spanish_enabled``, and the session id/token the tools send.
"""

from __future__ import annotations

from typing import Any, Literal

from .tool_catalog import pre_call_fetch_url

Product = Literal["CALLBACK_RECOVERY", "LEAD_QUALIFIER"]

GLOBAL_PROMPT = """# GLOBAL RULES (every step)

## Who you are
You are {{persona_name | Alex}}, a virtual assistant calling for {{agency_name | the agency}}.
You are NOT a licensed insurance agent and never say or imply you are. If asked, say:
"I'm a virtual assistant. I can connect you with a licensed agent."

## What you never do
- Never give insurance advice, recommend a product, or say what they qualify for.
- Never name a carrier, a price, a premium, a benefit amount or a waiting period.
  Say: "The licensed agent will go over exact options and prices with you."
- Never ask for or repeat a social security number, bank, card or health details.
- Never invent facts. If you don't know, say a licensed agent will answer that.
- Never keep the person on the phone longer than needed. The goal is a short,
  qualified conversation with a licensed agent, not a long one with you.

## The Hoppwhistle tools decide, you follow
Every tool answer has an `instruction`. Do exactly what it says:
- ask_next_question: ask ONLY the `next_question.ask` given, in your own words.
- offer_transfer: ask if they have a few minutes to talk with a licensed agent now;
  on yes, call transfer_to_agent immediately and say nothing more.
- offer_callback: explain briefly (the `message` says why) and offer a callback.
- confirm_callback: tell them the time given in `scheduled_for_local`.
- end_call_politely / end_call_wrong_person / end_call_dnc_confirmed: say one short
  closing line and call end_call.
Never say a tool name, a session id, or that you are "checking a system".

## Compliance stops
- "Stop calling", "take me off your list", "do not call", "remove me": call mark_dnc
  at once, confirm they won't be called again, then end_call (reason dnc_requested).
- Wrong person: call update_customer with wrong_person=true, share nothing about the
  prospect or why you called, apologize, end_call (reason wrong_person).
- Voicemail, an answering machine or a call screener: say nothing, end_call
  (reason voicemail).
- No response after two tries: end_call (reason no_response).

## Language
If they speak Spanish and {{spanish_enabled | false}} is true, continue in Spanish and
call update_customer with preferred_language "es". If it is false, apologize in Spanish
and offer a callback from an agent.
"""

PRODUCT_CONTEXT = {
    "CALLBACK_RECOVERY": (
        "You are calling back someone the agency missed or promised a callback. "
        "Reason on file: {{reason_for_call | they asked to be called back}}. "
        "Never say they asked for anything they did not; if they say they didn't call, "
        "agree and ask if they'd still like to look at final expense coverage."
    ),
    "LEAD_QUALIFIER": (
        "You are calling a new lead who recently asked about final expense (burial) "
        "coverage. Reason on file: {{reason_for_call | they requested information}}."
    ),
}


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


def build_workflow(
    product: Product,
    tools: dict[str, str],
    *,
    base_url: str,
    signing_secret: str | None,
    inbound: bool = False,
    credential_uuid: str | None = None,
) -> dict[str, Any]:
    """A workflow_definition for ``product`` wired to the installed tool UUIDs."""

    def refs(*names: str) -> list[str]:
        return [tools[name] for name in names if name in tools]

    if inbound:
        greeting = (
            "Thanks for calling {{agency_name | us}}. This is {{persona_name | Alex}}, a virtual "
            "assistant. Our licensed agents may be busy, but I can help. Who am I speaking with?"
        )
        opening = (
            "They called in when no agent could answer. If unattributed is true "
            "({{hw_unattributed | false}}), say the office is closed, ask them to call back "
            "during business hours, and end_call (reason no_response). Otherwise call "
            "lookup_customer first, then confirm their name."
        )
    else:
        greeting = (
            "Hi, this is {{persona_name | Alex}}, a virtual assistant calling for "
            "{{agency_name | the agency}}. May I speak with {{first_name | the person who "
            "requested information}}?"
        )
        opening = "Call lookup_customer first. Then confirm you are speaking with the prospect by first name."

    start_data: dict[str, Any] = {
        "name": "Greeting",
        "greeting_type": "text",
        "greeting": greeting,
        "prompt": (
            "# GREETING\n\n"
            f"{opening}\n\n"
            "When they confirm who they are, call save_qualification with "
            "identity_confirmed=true and move on. Wrong person, voicemail or a "
            "do-not-call request: follow the global rules."
        ),
        "allow_interrupt": True,
        "add_global_prompt": True,
        "delayed_start": True,
        "delayed_start_duration": 0.6,
        "extraction_enabled": False,
        "tool_uuids": refs(
            "lookup_customer",
            "update_customer",
            "save_qualification",
            "mark_dnc",
            "end_call",
        ),
        "is_start": True,
    }
    if inbound:
        start_data.update(
            {
                "pre_call_fetch_enabled": True,
                "pre_call_fetch_url": pre_call_fetch_url(base_url),
                "pre_call_fetch_credential_uuid": credential_uuid,
            }
        )

    nodes = [
        _node(
            "global",
            "globalNode",
            -40,
            -260,
            {
                "name": "Global",
                "prompt": f"{GLOBAL_PROMPT}\n## Why you are calling\n{PRODUCT_CONTEXT[product]}",
                "allow_interrupt": True,
                "add_global_prompt": False,
            },
        ),
        _node("greeting", "startCall", -160, 40, start_data),
        _node(
            "qualify",
            "agentNode",
            420,
            -80,
            {
                "name": "Qualify",
                "prompt": (
                    "# QUALIFY\n\n"
                    "Call save_qualification with every answer as soon as you hear it, and ask "
                    "only the question its answer names. Do not ask anything else. The moment "
                    "it says offer_transfer, offer the licensed agent; on yes, call "
                    "transfer_to_agent with a one-sentence reason. If they ask for a person "
                    "before finishing, call transfer_to_agent with prospect_requested_agent="
                    "true (the state is still needed). If they prefer later, call create_callback."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": False,
                "tool_uuids": refs(
                    "save_qualification",
                    "update_customer",
                    "eligible_agents",
                    "transfer_to_agent",
                    "create_callback",
                    "mark_dnc",
                    "end_call",
                ),
            },
        ),
        _node(
            "callback",
            "agentNode",
            1080,
            240,
            {
                "name": "Book Callback",
                "prompt": (
                    "# BOOK CALLBACK\n\n"
                    "A live transfer isn't possible right now. Ask when is a good time for a "
                    "licensed agent to call, call create_callback (callback_at as ISO 8601 with "
                    "their offset, or omit for as soon as possible), and confirm the time it "
                    "returns. Never promise a specific agent."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": False,
                "tool_uuids": refs("create_callback", "mark_dnc", "end_call"),
            },
        ),
        _node(
            "end",
            "endCall",
            580,
            580,
            {
                "name": "Wrap Up",
                "prompt": (
                    "# WRAP UP\n\nOne short, warm closing line. Do not say records were updated. "
                    "Then call end_call with the reason."
                ),
                "allow_interrupt": True,
                "add_global_prompt": True,
                "extraction_enabled": False,
                "tool_uuids": refs("end_call"),
                "is_end": True,
            },
        ),
    ]
    if signing_secret is not None:
        nodes.append(
            _node(
                "hoppwhistle",
                "hoppwhistle",
                -420,
                -260,
                {
                    "name": "Hoppwhistle",
                    "hoppwhistle_enabled": True,
                    "hoppwhistle_base_url": base_url.rstrip("/"),
                    "hoppwhistle_signing_secret": signing_secret,
                },
            )
        )

    edges = [
        _edge(
            "greeting",
            "qualify",
            "identity confirmed",
            "The person confirmed they are the prospect (or gave their name on an inbound call) "
            "and is willing to continue. Not on hello, who is this, busy, wrong number, "
            "voicemail or a do-not-call request.",
        ),
        _edge(
            "greeting",
            "end",
            "cannot continue",
            "Wrong person, voicemail or screener, do-not-call request, not available, "
            "unattributed after-hours call, or no response.",
        ),
        _edge(
            "qualify",
            "callback",
            "transfer not possible",
            "A tool said offer_callback, the transfer was declined or failed, or the person "
            "would rather be called later.",
            transition_speech="I'm sorry, I can't connect you with a licensed agent right this minute.",
            transition_speech_type="text",
        ),
        _edge(
            "qualify",
            "end",
            "done or not a fit",
            "They do not qualify, are not interested, asked for do-not-call, turned out to be "
            "the wrong person, or the conversation is otherwise finished.",
        ),
        _edge(
            "callback",
            "end",
            "callback handled",
            "A callback time was confirmed, or they declined a callback, or asked for do-not-call.",
        ),
    ]
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.8}}


WORKFLOW_NAMES = {
    ("CALLBACK_RECOVERY", False): "AI Callback Recovery (Hoppwhistle)",
    (
        "CALLBACK_RECOVERY",
        True,
    ): "AI Callback Recovery - After Hours Inbound (Hoppwhistle)",
    ("LEAD_QUALIFIER", False): "AI Lead Qualifier (Hoppwhistle)",
}
