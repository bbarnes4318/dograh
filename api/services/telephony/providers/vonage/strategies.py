"""Vonage-specific call operation strategies.

Caller-side leg of the conversation-based transfer. ``VonageProvider.
transfer_call`` already dialed the destination with an NCCO that joins the
named conversation on answer; when the pipeline ends with
``EndTaskReason.TRANSFER_CALL`` this strategy moves the caller leg into that
same conversation with ``PUT /v1/calls/{uuid}`` (``action: transfer``).

API reference: https://developer.vonage.com/en/api/voice#updateCall
"""

from typing import Any, Dict, Optional

from loguru import logger
from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy

from .client import VonageVoiceClient


def _client(context: Dict[str, Any]) -> VonageVoiceClient:
    return VonageVoiceClient(context.get("application_id"), context.get("private_key"))


async def _find_transfer_context(call_uuid: str):
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager

    try:
        manager = await get_call_transfer_manager()
        return await manager.find_transfer_context_for_call(call_uuid)
    except Exception as e:
        logger.error(
            f"provider=vonage call_uuid={call_uuid} transfer lookup failed: {e}"
        )
        return None


async def _remove_transfer_context(transfer_id: str) -> None:
    from api.services.telephony.call_transfer_manager import get_call_transfer_manager

    try:
        manager = await get_call_transfer_manager()
        await manager.remove_transfer_context(transfer_id)
    except Exception as e:
        logger.error(f"provider=vonage transfer_id={transfer_id} cleanup failed: {e}")


async def _hangup_quietly(client: VonageVoiceClient, call_uuid: Optional[str]) -> bool:
    if not call_uuid:
        return False
    try:
        return await client.hangup(call_uuid)
    except Exception as e:
        logger.error(f"provider=vonage call_uuid={call_uuid} hangup failed: {e}")
        return False


class VonageConversationTransferStrategy(TransferStrategy):
    """Moves the caller leg into the transfer conversation."""

    async def execute_transfer(self, context: Dict[str, Any]) -> bool:
        call_uuid = context.get("call_uuid")
        client = _client(context)

        transfer_context = await _find_transfer_context(call_uuid)
        if not transfer_context:
            logger.error(
                f"provider=vonage call_uuid={call_uuid} no active transfer context"
            )
            return False

        conference_name = transfer_context.conference_name
        ctx = (
            f"provider=vonage call_uuid={call_uuid} "
            f"transfer_id={transfer_context.transfer_id} conference={conference_name}"
        )
        ncco = [
            {
                "action": "conversation",
                "name": conference_name,
                "startOnEnter": True,
                "endOnExit": True,
            }
        ]
        try:
            await client.transfer_to_ncco(call_uuid, ncco)
        except Exception as e:
            # The caller can't be bridged (usually: already hung up). Hang up
            # the destination so it isn't left alone in the conversation.
            logger.error(f"{ctx} caller transfer failed: {e}; releasing destination")
            await _hangup_quietly(client, transfer_context.call_sid)
            await _remove_transfer_context(transfer_context.transfer_id)
            return False

        logger.info(f"{ctx} caller joined transfer conversation")
        await _remove_transfer_context(transfer_context.transfer_id)
        return True


class VonageHangupStrategy(HangupStrategy):
    """Hangs up the call leg, plus any transfer leg still pending for it."""

    async def execute_hangup(self, context: Dict[str, Any]) -> bool:
        call_uuid = context.get("call_uuid")
        client = _client(context)
        if not call_uuid:
            logger.warning("provider=vonage cannot hang up: missing call_uuid")
            return False

        # A pipeline that ends while a transfer is still ringing (caller hung
        # up, error, duration limit) must not leave the destination leg
        # dialing into an empty conversation.
        transfer_context = await _find_transfer_context(call_uuid)
        if transfer_context:
            logger.info(
                f"provider=vonage call_uuid={call_uuid} releasing pending transfer "
                f"leg {transfer_context.call_sid}"
            )
            await _hangup_quietly(client, transfer_context.call_sid)
            await _remove_transfer_context(transfer_context.transfer_id)

        ok = await _hangup_quietly(client, call_uuid)
        if ok:
            logger.info(f"provider=vonage call_uuid={call_uuid} hung up")
        return ok
