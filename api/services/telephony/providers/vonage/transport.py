"""Vonage transport factory."""

from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.audio_mixer import build_audio_out_mixer
from api.services.pipecat.transport_params import (
    audio_in_param_overrides,
    realtime_param_overrides,
)
from api.services.telephony.factory import load_credentials_for_transport

from .serializers import VonageFrameSerializer
from .strategies import VonageConversationTransferStrategy, VonageHangupStrategy

# Vonage's WebSocket expects 20 ms binary frames (640 bytes at 16 kHz L16);
# its playback buffer is counted in these packets.
VONAGE_AUDIO_OUT_10MS_CHUNKS = 2


async def create_transport(
    websocket,
    workflow_run_id: int,
    audio_config: AudioConfig,
    organization_id: int,
    *,
    ambient_noise_config: dict | None = None,
    noise_suppression_config: dict | None = None,
    telephony_configuration_id: int | None = None,
    is_realtime: bool = False,
    call_uuid: str,
):
    """Create a transport for Vonage connections."""
    config = await load_credentials_for_transport(
        organization_id, telephony_configuration_id, expected_provider="vonage"
    )

    application_id = config.get("application_id")
    private_key = config.get("private_key")

    if not application_id or not private_key:
        raise ValueError(
            f"Incomplete Vonage configuration for organization {organization_id}"
        )

    # Wire format and pipeline both run at 16 kHz (ProviderSpec
    # transport_sample_rate), so the serializer's resamplers are pass-through:
    # no duplicate resampling and no mu-law step — raw s16le PCM both ways.
    serializer = VonageFrameSerializer(
        call_uuid=call_uuid,
        application_id=application_id,
        private_key=private_key,
        params=VonageFrameSerializer.InputParams(
            vonage_sample_rate=audio_config.transport_in_sample_rate,
            sample_rate=audio_config.pipeline_sample_rate,
        ),
        transfer_strategy=VonageConversationTransferStrategy(),
        hangup_strategy=VonageHangupStrategy(),
    )

    mixer = await build_audio_out_mixer(
        audio_config.transport_out_sample_rate, ambient_noise_config
    )

    # Vonage uses binary WebSocket mode, not text
    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_config.transport_in_sample_rate,
            audio_out_sample_rate=audio_config.transport_out_sample_rate,
            audio_out_10ms_chunks=VONAGE_AUDIO_OUT_10MS_CHUNKS,
            audio_out_mixer=mixer,
            serializer=serializer,
            **audio_in_param_overrides(noise_suppression_config),
            **realtime_param_overrides(is_realtime),
        ),
    )
