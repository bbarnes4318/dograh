"""The agent-callable side of suppression.

When a caller says "take me off your list", the model can act on it during the
call instead of leaving it to a disposition that someone has to notice later.

The tool deliberately takes **no phone number**. It always suppresses the
number on the current call. Letting the model pass one invites a misheard digit
string — or the agent's own caller ID, read back from the transcript — onto the
list, and a wrong entry there silently stops a campaign. Numbers other than the
one calling are added through the API, by a person.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from api.services.dnc.service import dnc_service
from api.services.dnc.suppression import counterparty_number

TOOL_NAME = "add_to_do_not_call"


def get_add_to_dnc_tool_schema():
    """Function schema for the agent-callable suppression request."""
    from api.services.workflow.pipecat_engine_custom_tools import get_function_schema

    return get_function_schema(
        TOOL_NAME,
        (
            "Record that this caller does not want to be contacted again. Call "
            "this as soon as they ask to be removed from the list, to stop "
            "calling, or say anything else meaning they want no further "
            "contact. It applies to the number on this call — you do not need "
            "to ask them for it. Acknowledge the request after calling this."
        ),
        properties={
            "reason": {
                "type": "string",
                "description": (
                    "What the caller actually said, in a few words, so an "
                    "operator reviewing the list can see why the number is on "
                    "it. For example: 'asked to be removed from the list'."
                ),
            }
        },
        required=[],
    )


async def add_caller_to_dnc(
    *,
    organization_id: int | None,
    call_context_vars: dict | None,
    reason: str | None = None,
    workflow_run_id: int | None = None,
) -> dict[str, Any]:
    """Suppress the number on the current call.

    Returns a result the model can react to verbally, so it doesn't promise
    something that didn't happen.
    """
    if not organization_id:
        logger.error("Cannot add to do-not-call list: organization_id missing")
        return {
            "status": "error",
            "error": "This call is not associated with an organization.",
        }

    number = counterparty_number(call_context_vars)
    if not number:
        logger.error(
            f"Cannot add to do-not-call list for run {workflow_run_id}: "
            "no counterparty number on the call context"
        )
        return {
            "status": "error",
            "error": "This call has no phone number to suppress.",
        }

    result = await dnc_service.add_from_agent(
        organization_id=organization_id,
        raw_number=number,
        reason=reason,
        workflow_run_id=workflow_run_id,
    )

    if not result.accepted:
        logger.error(
            f"Could not suppress {number} for org {organization_id}: "
            f"not a usable phone number"
        )
        return {
            "status": "error",
            "error": "That number could not be added to the do-not-call list.",
        }

    logger.info(
        f"Caller on run {workflow_run_id} added to do-not-call list "
        f"for org {organization_id}"
    )
    return {
        "status": "ok",
        "already_listed": result.already_listed > 0,
    }
