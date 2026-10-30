"""One inbound call per customer number.

An AI agent answers a customer's number once. When the same number calls any
of the organization's AI agents again, the call is hung up before it is
answered. This covers every inbound path into Dograh (ARI and provider
webhooks); it does not touch outbound calls or anything outside Dograh.

A handoff from one AI agent to another is never refused. It reaches the
second agent as a new inbound call, and when the transfer carries the lead
(``.../sip:+1<DID>*{{called_number}}*{{caller_number}}*...``) its caller ID is
the customer's own number. It is recognised by either of:

- the customer is on a call with one of the org's AI agents right now (the
  run doing the transfer has not ended yet);
- the caller ID is one of the org's own phone numbers (a transfer that did
  not carry the lead shows our trunk's number).

Such a call is answered and its run is marked ``ai_transfer``, so it is never
counted as the customer's own call.

Only answered calls count: a call refused here, or for any other reason
before a run was created, does not use up the customer's one call.

``INBOUND_ONE_CALL_PER_CALLER=false`` turns the rule off. Withheld numbers,
SIP URIs and anything that is not a US number are not checked.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

from loguru import logger

from api.db import db_client
from api.services.telephony.blocked_numbers import ten_digits

# Key on a run's initial_context marking an AI-to-AI handoff.
AI_TRANSFER_KEY = "ai_transfer"

# How far back a not-yet-completed run counts as a call in progress. Bounds
# the damage of a run that never got marked completed.
LIVE_CALL_WINDOW = timedelta(hours=2)


@dataclass(frozen=True)
class InboundCallerDecision:
    admit: bool
    ai_transfer: bool = False
    reason: str = ""


def rule_enabled(env: Optional[dict] = None) -> bool:
    source = os.environ if env is None else env
    return source.get("INBOUND_ONE_CALL_PER_CALLER", "true").strip().lower() != "false"


async def check_inbound_caller(
    organization_id: int, caller_number: Optional[str]
) -> InboundCallerDecision:
    """Whether an AI agent may answer this inbound call, and whether it is a
    handoff from another AI agent.

    If the database can't be read, the call is answered: a hiccup should not
    drop customers' calls.
    """
    if not rule_enabled():
        return InboundCallerDecision(admit=True)
    digits = ten_digits(caller_number)
    if digits is None:
        return InboundCallerDecision(admit=True)

    try:
        if await db_client.is_organization_phone_number(organization_id, f"+1{digits}"):
            return InboundCallerDecision(
                admit=True, ai_transfer=True, reason="caller is one of our numbers"
            )
        if await db_client.number_on_live_call(
            organization_id, digits, datetime.now(UTC) - LIVE_CALL_WINDOW
        ):
            return InboundCallerDecision(
                admit=True,
                ai_transfer=True,
                reason="caller is on a call with an AI agent (AI transfer)",
            )
        if await db_client.inbound_caller_has_answered_call(organization_id, digits):
            return InboundCallerDecision(
                admit=False, reason="this number has already called an AI agent"
            )
    except Exception as e:
        logger.warning(
            f"Could not check earlier calls from {caller_number} for org "
            f"{organization_id}; answering: {e}"
        )
    return InboundCallerDecision(admit=True)
