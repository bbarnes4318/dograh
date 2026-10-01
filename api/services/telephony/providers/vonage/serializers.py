"""Vonage frame serializer.

Pipecat's ``VonageFrameSerializer`` handles the wire format (binary 16-bit
little-endian linear PCM, ``{"action": "clear"}`` on interruption, JSON
events) but has no transfer/hangup strategy hooks. This subclass adds them so
the generic Transfer Call tool works on Vonage, mirroring how the Twilio and
Telnyx serializers dispatch on ``EndTaskReason.TRANSFER_CALL``.
"""

from typing import Optional

from loguru import logger
from pipecat.frames.frames import CancelFrame, EndFrame, Frame
from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy
from pipecat.serializers.vonage import VonageFrameSerializer as _PipecatVonageSerializer
from pipecat.utils.enums import EndTaskReason


class VonageFrameSerializer(_PipecatVonageSerializer):
    """Vonage Voice API WebSocket serializer with transfer/hangup strategies."""

    def __init__(
        self,
        call_uuid: str,
        application_id: Optional[str] = None,
        private_key: Optional[str] = None,
        params: Optional[_PipecatVonageSerializer.InputParams] = None,
        transfer_strategy: Optional[TransferStrategy] = None,
        hangup_strategy: Optional[HangupStrategy] = None,
    ):
        super().__init__(
            call_uuid=call_uuid,
            application_id=application_id,
            private_key=private_key,
            params=params,
        )
        self._transfer_strategy = transfer_strategy
        self._hangup_strategy = hangup_strategy
        self._transfer_attempted = False

    def _strategy_context(self) -> dict:
        return {
            "call_uuid": self._call_uuid,
            "application_id": self._application_id,
            "private_key": self._private_key,
        }

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if not isinstance(frame, (EndFrame, CancelFrame)):
            return await super().serialize(frame)

        reason = getattr(frame, "reason", None)
        if reason == EndTaskReason.TRANSFER_CALL.value and not self._transfer_attempted:
            self._transfer_attempted = True
            # Once the caller leg is handed to the transfer conversation it
            # must never be hung up by a later End/Cancel frame. If the
            # transfer fails, the leg ends by itself when this websocket
            # closes (its NCCO has nothing after ``connect``).
            self._hangup_attempted = True
            if self._transfer_strategy:
                ok = await self._transfer_strategy.execute_transfer(
                    self._strategy_context()
                )
                if not ok:
                    logger.error(
                        f"provider=vonage call_uuid={self._call_uuid} transfer "
                        "strategy failed"
                    )
            else:
                logger.warning(
                    f"provider=vonage call_uuid={self._call_uuid} no transfer "
                    "strategy configured"
                )
            return None

        if self._params.auto_hang_up and not self._hangup_attempted:
            self._hangup_attempted = True
            if self._hangup_strategy:
                ok = await self._hangup_strategy.execute_hangup(
                    self._strategy_context()
                )
                if not ok:
                    logger.error(
                        f"provider=vonage call_uuid={self._call_uuid} hangup "
                        "strategy failed"
                    )
            else:
                await self._hang_up_call()
        return None


__all__ = ["VonageFrameSerializer"]
