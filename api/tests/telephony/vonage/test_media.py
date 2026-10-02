"""Vonage media path: serializer wire format and transport configuration."""

import json
import struct
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)

from api.services.pipecat.audio_config import create_audio_config
from api.services.telephony.providers.vonage.serializers import VonageFrameSerializer


def _pcm(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


async def _serializer(rate=16000):
    s = VonageFrameSerializer(
        call_uuid="call",
        params=VonageFrameSerializer.InputParams(
            vonage_sample_rate=rate, sample_rate=rate
        ),
    )
    await s.setup(StartFrame(audio_in_sample_rate=rate, audio_out_sample_rate=rate))
    return s


def test_audio_config_is_16k_end_to_end():
    cfg = create_audio_config("vonage")
    assert cfg.transport_in_sample_rate == 16000
    assert cfg.transport_out_sample_rate == 16000
    assert cfg.pipeline_sample_rate == 16000
    assert cfg.vad_sample_rate == 16000


async def test_inbound_audio_is_passed_through_as_16k_pcm():
    s = await _serializer()
    payload = _pcm([0, 1000, -1000, 32767, -32768] * 64)  # 320 samples = 20 ms
    frame = await s.deserialize(payload)
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 16000
    assert frame.num_channels == 1
    # Same rate on both sides: byte-exact, no resampling, no mu-law step.
    assert frame.audio == payload


async def test_outbound_audio_is_raw_little_endian_pcm():
    s = await _serializer()
    payload = _pcm([1, -2, 300, -400] * 160)  # 640 samples
    out = await s.serialize(
        OutputAudioRawFrame(audio=payload, sample_rate=16000, num_channels=1)
    )
    assert isinstance(out, bytes)
    assert out == payload
    assert struct.unpack("<4h", out[:8]) == (1, -2, 300, -400)


async def test_outbound_audio_at_other_rate_is_resampled_once_to_16k():
    s = await _serializer()
    payload = _pcm([0] * 2400)  # 100 ms at 24 kHz
    out = b""
    for _ in range(5):  # stream resampler may buffer the first chunk
        chunk = await s.serialize(
            OutputAudioRawFrame(audio=payload, sample_rate=24000, num_channels=1)
        )
        out += chunk or b""
    # 5 x 100 ms at 16 kHz s16 = 16000 bytes (allow resampler latency)
    assert 12000 <= len(out) <= 16000
    assert len(out) % 2 == 0


async def test_interruption_sends_clear_for_barge_in():
    s = await _serializer()
    assert json.loads(await s.serialize(InterruptionFrame())) == {"action": "clear"}


async def test_dtmf_event_is_decoded():
    s = await _serializer()
    frame = await s.deserialize(json.dumps({"event": "websocket:dtmf", "digit": "5"}))
    assert isinstance(frame, InputDTMFFrame)
    assert frame.button.value == "5"


@pytest.mark.parametrize(
    "message",
    [
        {"event": "websocket:connected", "content-type": "audio/l16;rate=16000"},
        {"event": "websocket:cleared"},
        {"event": "websocket:notify", "payload": {}},
    ],
)
async def test_control_events_produce_no_frames(message):
    s = await _serializer()
    assert await s.deserialize(json.dumps(message)) is None


async def test_malformed_text_is_ignored_not_fatal():
    s = await _serializer()
    assert await s.deserialize("{oops") is None


async def test_create_transport_uses_20ms_frames_and_strategies(vonage_config):
    from api.services.telephony.providers.vonage import transport as transport_module
    from api.services.telephony.providers.vonage.strategies import (
        VonageConversationTransferStrategy,
        VonageHangupStrategy,
    )

    ws = AsyncMock()
    with (
        patch.object(
            transport_module,
            "load_credentials_for_transport",
            new=AsyncMock(return_value=vonage_config()),
        ) as load_creds,
        patch.object(
            transport_module, "build_audio_out_mixer", new=AsyncMock(return_value=None)
        ),
    ):
        transport = await transport_module.create_transport(
            ws,
            123,
            create_audio_config("vonage"),
            11,
            telephony_configuration_id=5,
            call_uuid="call-uuid",
        )

    load_creds.assert_awaited_once_with(11, 5, expected_provider="vonage")
    params = transport._params
    assert params.audio_in_sample_rate == 16000
    assert params.audio_out_sample_rate == 16000
    # 2 x 10 ms at 16 kHz s16 mono = 640-byte Vonage packets.
    assert params.audio_out_10ms_chunks == 2
    serializer = params.serializer
    assert isinstance(serializer, VonageFrameSerializer)
    assert serializer._call_uuid == "call-uuid"
    assert isinstance(serializer._transfer_strategy, VonageConversationTransferStrategy)
    assert isinstance(serializer._hangup_strategy, VonageHangupStrategy)


async def test_create_transport_rejects_wrong_provider_config():
    from api.services.telephony.providers.vonage import transport as transport_module

    with patch.object(
        transport_module,
        "load_credentials_for_transport",
        new=AsyncMock(side_effect=ValueError("Expected vonage provider, got twilio")),
    ):
        with pytest.raises(ValueError, match="Expected vonage"):
            await transport_module.create_transport(
                AsyncMock(), 1, create_audio_config("vonage"), 11, call_uuid="c"
            )
