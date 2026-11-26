"""The recruiting tools, as ordinary Dograh tool definitions.

Each is a plain ``http_api`` tool posting to Hoppwhistle's
``/api/internal/recruiting-voice/tools/<name>`` (or, for a person, a
``transfer_call`` tool with a dynamic resolver), so they run on Dograh's
existing tool machinery. Every tool sends two preset parameters the LLM
never sees and cannot override (``{**llm_args, **presets}``):

    session_id     {{initial_context.hr_session_id}}
    session_token  {{initial_context.hr_session_token}}

Hoppwhistle puts both in the call's initial context, and the session decides
the agency, the candidate and the application. The sales tools of the
``hoppwhistle`` package are never part of this set.

Tool names here must match Hoppwhistle's RECRUITING_VOICE_TOOL_NAMES
(packages/shared/src/recruiting/voice/session.ts) exactly.
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "2026-11-26"

#: Category the recruiting tools are installed under (with their names, the install key).
CATEGORY_HTTP = "http_api"

SESSION_PRESETS: list[dict[str, Any]] = [
    {
        "name": "session_id",
        "type": "string",
        "value_template": "{{initial_context.hr_session_id}}",
        "required": True,
    },
    {
        "name": "session_token",
        "type": "string",
        "value_template": "{{initial_context.hr_session_token}}",
        "required": True,
    },
]


def _p(
    name: str, type_: str, description: str, required: bool = False
) -> dict[str, Any]:
    return {
        "name": name,
        "type": type_,
        "description": description,
        "required": required,
    }


#: name -> (description shown to the LLM, LLM parameters)
HTTP_TOOLS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "get_interview_context": (
        "Call once at the start, right after the greeting. Returns the candidate's first name, the "
        "position, which interview steps are already done (a resumed interview) and what to do next.",
        [],
    ),
    "get_approved_agency_fact": (
        "Look up APPROVED agency facts before answering any question about compensation, leads, lead "
        "costs, carriers, products, training, technology, schedule, contracting, book of business, "
        "chargebacks or the agency. State only what it returns. If it returns nothing, say a recruiter "
        "will follow up (a task is created for you).",
        [
            _p(
                "topic",
                "string",
                "One word or short phrase, e.g. 'lead_cost', 'carriers', 'chargebacks'.",
                True,
            ),
            _p("question", "string", "The candidate's exact question."),
        ],
    ),
    "get_position_requirements": (
        "The position's stated requirements: license, states, work mode, hours, skills.",
        [],
    ),
    "save_candidate_reported_fact": (
        "Save a fact the candidate told you (licensing, background, a production figure, a preference). "
        "For numbers, also give what it is out of and the period. Set correction=true when they correct "
        "something they said earlier. Never save identification, bank, health, age or family details.",
        [
            _p(
                "fact",
                "string",
                "snake_case name, e.g. 'resident_state', 'weekly_apps_submitted', 'career_priority'.",
                True,
            ),
            _p("value", "string", "What they said.", True),
            _p("denominator", "string", "For a rate or count: what it is out of."),
            _p("period", "string", "For a figure: the period it covers."),
            _p("correction", "boolean", "True when this corrects an earlier answer."),
        ],
    ),
    "save_interview_answer": (
        "After each interview question, save a faithful summary of the answer in the candidate's words, "
        "using the question id shown in the step. Set declined=true if they chose not to answer, "
        "technical_issue=true if the audio failed.",
        [
            _p(
                "question_id",
                "string",
                "The id shown in brackets, e.g. 'cmp_omit_condition'.",
                True,
            ),
            _p("answer", "string", "A faithful summary in their words."),
            _p("declined", "boolean", "They chose not to answer."),
            _p(
                "technical_issue",
                "boolean",
                "The answer was lost to a technical problem.",
            ),
        ],
    ),
    "record_interview_evidence": (
        "Save a short VERBATIM quote supporting an answer (the candidate's exact words).",
        [
            _p("question_id", "string", "The question the quote belongs to.", True),
            _p("quote", "string", "Their exact words, one or two sentences.", True),
            _p("at_seconds", "number", "Seconds into the call, if known."),
        ],
    ),
    "mark_module_complete": (
        "Call when a step is finished, with the step's key, then move on.",
        [_p("module", "string", "The step key, e.g. 'background'.", True)],
    ),
    "get_recruiter_availability": (
        "Open times for a call with the recruiting team this week. Offer two or three.",
        [_p("preferred_text", "string", "What the candidate said about timing.")],
    ),
    "schedule_recruiter_meeting": (
        "Book a call with the recruiting team at one of the returned times, ONLY after the candidate "
        "confirmed it (confirmed=true).",
        [
            _p("starts_at", "string", "The exact starts_at value returned.", True),
            _p("confirmed", "boolean", "The candidate confirmed this time.", True),
        ],
    ),
    "schedule_requested_callback": (
        "The candidate wants to continue later or be called back. Give callback_at as ISO 8601 with an "
        "offset when they name a time. Tell them the time the response gives.",
        [
            _p("callback_at", "string", "ISO 8601 time with offset."),
            _p(
                "callback_time_text",
                "string",
                "What they said, e.g. 'tomorrow after 5'.",
            ),
            _p("reason", "string", "Why, briefly."),
        ],
    ),
    "send_approved_opportunity_summary": (
        "Send the candidate a link to their status page with the approved opportunity details "
        "(and the opportunity calculator when include_calculator=true).",
        [
            _p("topics", "string", "Topics they cared about, comma separated."),
            _p("include_calculator", "boolean", "Include the opportunity calculator."),
        ],
    ),
    "create_recruiter_followup_task": (
        "Create a follow-up for the recruiting team: a question you could not answer "
        "(kind CANDIDATE_QUESTION, with their exact words), a request for a person (HUMAN_INTERVIEW), "
        "an accommodation or different format (ALTERNATIVE_FORMAT), a callback (CALLBACK) or OTHER.",
        [
            _p(
                "kind",
                "string",
                "CANDIDATE_QUESTION, HUMAN_INTERVIEW, ALTERNATIVE_FORMAT, CALLBACK or OTHER.",
                True,
            ),
            _p(
                "detail",
                "string",
                "What is needed, with the candidate's exact words for a question.",
                True,
            ),
        ],
    ),
    "record_candidate_interest": (
        "Record what the candidate said about their interest. Interest is never acceptance of anything.",
        [
            _p(
                "interest",
                "string",
                "INTERESTED, WANTS_RECRUITER_CONVERSATION, WANTS_COMPENSATION_DETAILS, WANTS_WRITTEN_INFORMATION, "
                "WANTS_ANOTHER_CONVERSATION, NEEDS_TIME, NOT_INTERESTED or WITHDREW.",
                True,
            ),
            _p("note", "string", "Their words, briefly."),
        ],
    ),
    "record_consent_change": (
        "Call when the candidate agrees to recording on this call (granted=true) or declines or "
        "withdraws recording or the AI call (granted=false). On granted=false stop the interview.",
        [
            _p("consent", "string", "RECORDING or VOICE_AI.", True),
            _p(
                "granted",
                "boolean",
                "True if they agreed, false if they declined or withdrew.",
                True,
            ),
        ],
    ),
    "record_contact_opt_out": (
        "Call IMMEDIATELY when the candidate asks not to be contacted again. Calls, texts and email stop.",
        [_p("reason", "string", "Their words, briefly.")],
    ),
    "withdraw_application": (
        "Withdraw the candidate's application. First call with confirmed=false and ask them to confirm; "
        "call again with confirmed=true only after they clearly confirm.",
        [
            _p(
                "confirmed",
                "boolean",
                "The candidate clearly confirmed they want to withdraw.",
                True,
            ),
            _p("reason", "string", "Their reason, if they gave one."),
        ],
    ),
    "complete_interview": (
        "Call in the wrap-up, before end_call, with the reason the interview ended.",
        [
            _p(
                "reason",
                "string",
                "INTERVIEW_COMPLETED, CANDIDATE_ENDED_EARLY, CANDIDATE_BUSY, CALLBACK_REQUESTED, HUMAN_REQUESTED, RECORDING_DECLINED, WITHDREW, OPTED_OUT, WRONG_PERSON or POOR_AUDIO.",
            ),
            _p(
                "summary_for_recruiter",
                "string",
                "Two or three neutral sentences of what was covered. Never a recommendation.",
            ),
        ],
    ),
}

TRANSFER_TOOL = "request_human_recruiter"
END_TOOL = "end_interview"
TOOL_NAMES: tuple[str, ...] = (*HTTP_TOOLS.keys(), TRANSFER_TOOL, END_TOOL)

#: Name prefix that keeps recruiting tools apart from any same-named tool in the organization.
INSTALL_NAME_PREFIX = ""


def _http_tool(base_url: str, credential_uuid: str | None, name: str) -> dict[str, Any]:
    description, parameters = HTTP_TOOLS[name]
    return {
        "name": name,
        "description": description,
        "category": CATEGORY_HTTP,
        "icon": "briefcase",
        "icon_color": "#4338CA",
        "definition": {
            "schema_version": 1,
            "type": "http_api",
            "config": {
                "method": "POST",
                "url": f"{base_url}/api/internal/recruiting-voice/tools/{name}",
                "credential_uuid": credential_uuid,
                "parameters": parameters or None,
                "preset_parameters": SESSION_PRESETS,
                "timeout_ms": 6000,
            },
        },
    }


def _transfer_tool(base_url: str, credential_uuid: str | None) -> dict[str, Any]:
    return {
        "name": TRANSFER_TOOL,
        "description": (
            "Connect the candidate with someone from the recruiting team when they ask for a person. "
            "Hoppwhistle decides whether anyone can take the call; if not, the transfer is declined "
            "with what to say."
        ),
        "category": "transfer_call",
        "icon": "phone-forwarded",
        "icon_color": "#4338CA",
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
                    "url": f"{base_url}/api/internal/recruiting-voice/transfer",
                    "credential_uuid": credential_uuid,
                    "timeout_ms": 4000,
                    "parameters": [
                        _p("reason", "string", "Why they want a person, briefly.")
                    ],
                    "preset_parameters": SESSION_PRESETS,
                },
            },
        },
    }


def _end_call_tool() -> dict[str, Any]:
    return {
        "name": END_TOOL,
        "description": "End the call after your closing line.",
        "category": "end_call",
        "icon": "phone-off",
        "icon_color": "#B91C1C",
        "definition": {
            "schema_version": 1,
            "type": "end_call",
            "config": {
                "messageType": "none",
                "endCallReason": True,
                "endCallReasonDescription": "Why the call ended, in a few words.",
            },
        },
    }


def build_tool_definitions(
    base_url: str, credential_uuid: str | None
) -> list[dict[str, Any]]:
    """Every recruiting tool, ready for ``CreateToolRequest``."""
    base = base_url.rstrip("/")
    return [
        *(_http_tool(base, credential_uuid, name) for name in HTTP_TOOLS),
        _transfer_tool(base, credential_uuid),
        _end_call_tool(),
    ]


def is_recruiting_tool(name: str, category: str, url: str | None = None) -> bool:
    """Whether an installed tool is one of ours (by name, category and target path)."""
    if name == END_TOOL:
        return category == "end_call"  # the engine's end-call tool type
    if name == TRANSFER_TOOL:
        return category == "transfer_call"
    return (
        name in HTTP_TOOLS
        and category == CATEGORY_HTTP
        and (url is None or "/api/internal/recruiting-voice/tools/" in url)
    )
