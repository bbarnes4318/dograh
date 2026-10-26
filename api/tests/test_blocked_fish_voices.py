"""The removed local-accent Fish voices can never reach a call.

Every blocked id (and the old auto:* picker values) must be swapped for the
standard voice wherever a Fish voice is loaded or a Fish service is built.
"""

from types import SimpleNamespace

import pytest

from api.services.configuration.registry import FishAudioTTSConfiguration
from api.services.pipecat.service_factory import create_tts_service
from api.services.voice_routing.blocked_voices import (
    BLOCKED_FISH_VOICES,
    DEFAULT_FISH_VOICE,
    is_blocked_fish_voice,
)

# One removed accent voice (East Tennessee, female) and one allowed voice.
REMOVED_VOICE = "bdb20464e82b497bb26951af016be86b"
OTHER_VOICE = "0123456789abcdef0123456789abcdef"


def _build_fish(voice):
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider="fish",
            api_key="test-key",
            model="s2.1-pro",
            voice=voice,
            latency="balanced",
            speed=None,
            volume=None,
            normalize=True,
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=8000, transport_in_sample_rate=8000
    )
    return create_tts_service(user_config, audio_config)


def test_all_removed_voices_and_auto_values_are_blocked():
    assert len(BLOCKED_FISH_VOICES) == 152
    assert {"auto:female", "auto:male", REMOVED_VOICE} <= BLOCKED_FISH_VOICES
    assert not is_blocked_fish_voice(DEFAULT_FISH_VOICE)
    assert is_blocked_fish_voice(f"  {REMOVED_VOICE.upper()} ")


@pytest.mark.parametrize("voice", sorted(BLOCKED_FISH_VOICES))
def test_factory_never_sends_a_blocked_voice_to_fish(voice):
    assert _build_fish(voice)._settings.voice == DEFAULT_FISH_VOICE


@pytest.mark.parametrize("voice", ["", None])
def test_factory_uses_standard_voice_when_none_is_set(voice):
    assert _build_fish(voice)._settings.voice == DEFAULT_FISH_VOICE


def test_factory_keeps_an_allowed_voice():
    assert _build_fish(OTHER_VOICE)._settings.voice == OTHER_VOICE


def test_saved_config_with_removed_voice_loads_as_standard_voice():
    assert (
        FishAudioTTSConfiguration(api_key="k", voice=REMOVED_VOICE).voice
        == DEFAULT_FISH_VOICE
    )
    assert (
        FishAudioTTSConfiguration(api_key="k", voice="auto:male").voice
        == DEFAULT_FISH_VOICE
    )
    assert FishAudioTTSConfiguration(api_key="k").voice == DEFAULT_FISH_VOICE
    assert FishAudioTTSConfiguration(api_key="k", voice=OTHER_VOICE).voice == (
        OTHER_VOICE
    )
