"""Configuration tests for OpenAI GPT-Live / Realtime / TTS voices."""

import pytest
from pydantic import ValidationError

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.masking import mask_user_config
from api.services.configuration.options.openai import (
    OPENAI_LIVE_DEFAULT_VOICE,
    OPENAI_LIVE_VOICE_OPTIONS,
    OPENAI_LIVE_VOICES,
    OPENAI_REALTIME_VOICES,
    OPENAI_TTS_VOICES,
    openai_voice_param,
)
from api.services.configuration.registry import (
    REALTIME_PROVIDERS,
    OpenAILiveLLMConfiguration,
    OpenAILLMService,
    OpenAIRealtimeLLMConfiguration,
    OpenAITTSService,
    ServiceProviders,
)
from api.services.configuration.resolve import resolve_effective_config


def _effective(realtime, is_realtime=True):
    return EffectiveAIModelConfiguration(
        llm=OpenAILLMService(api_key="sk-llm-secret-1234567890", model="gpt-4.1"),
        tts=OpenAITTSService(api_key="sk-tts-secret-1234567890"),
        stt={
            "provider": "deepgram",
            "api_key": "dg-secret-1234567890",
            "model": "nova-3",
        },
        realtime=realtime,
        is_realtime=is_realtime,
    )


class TestGptLive:
    def test_defaults(self):
        cfg = OpenAILiveLLMConfiguration(api_key="k")
        assert cfg.provider == ServiceProviders.OPENAI_LIVE
        assert cfg.model == "gpt-live-1"
        assert cfg.voice == OPENAI_LIVE_DEFAULT_VOICE == "meridian"
        assert cfg.voice_type == "builtin"

    @pytest.mark.parametrize("voice", OPENAI_LIVE_VOICES)
    def test_every_builtin_voice_accepted(self, voice):
        assert OpenAILiveLLMConfiguration(api_key="k", voice=voice).voice == voice

    def test_documented_voices_present(self):
        required = {
            "gleam", "meridian", "delta", "cinder", "quartz", "ripple",
            "vesper", "willow", "stone", "beacon", "bossa", "tempo",
        }  # fmt: skip
        assert required <= set(OPENAI_LIVE_VOICES)

    def test_invalid_voice_rejected(self):
        with pytest.raises(ValidationError):
            OpenAILiveLLMConfiguration(api_key="k", voice="alloy-not-a-live-voice")

    def test_voice_catalog_groups_and_descriptions(self):
        by_id = {v.id: v for v in OPENAI_LIVE_VOICE_OPTIONS}
        assert by_id["meridian"].group == "North American"
        assert "masculine" in by_id["meridian"].description
        assert by_id["delta"].group == "Southern U.S."
        assert by_id["vesper"].group == "British / Irish / Australian"
        assert by_id["bossa"].group == "Other"
        schema = OpenAILiveLLMConfiguration.model_json_schema()
        assert schema["properties"]["voice"]["voice_catalog"][0]["group"]

    def test_registered_as_realtime_provider(self):
        assert ServiceProviders.OPENAI_LIVE.value in REALTIME_PROVIDERS


class TestRealtime:
    def test_default_voice_is_marin_and_cedar_listed_first(self):
        cfg = OpenAIRealtimeLLMConfiguration(api_key="k")
        assert cfg.voice == "marin"
        assert OPENAI_REALTIME_VOICES[:2] == ("marin", "cedar")
        assert cfg.model == "gpt-realtime-2"

    @pytest.mark.parametrize("voice", OPENAI_REALTIME_VOICES)
    def test_voice_accepted(self, voice):
        assert OpenAIRealtimeLLMConfiguration(api_key="k", voice=voice).voice == voice

    def test_tts_only_voice_rejected_for_realtime(self):
        with pytest.raises(ValidationError):
            OpenAIRealtimeLLMConfiguration(api_key="k", voice="fable")


class TestTTS:
    @pytest.mark.parametrize("voice", OPENAI_TTS_VOICES)
    def test_voice_accepted(self, voice):
        assert OpenAITTSService(api_key="k", voice=voice).voice == voice

    def test_thirteen_voices_marin_cedar_first(self):
        assert len(OPENAI_TTS_VOICES) == 13
        assert OPENAI_TTS_VOICES[:2] == ("marin", "cedar")

    def test_invalid_voice_rejected(self):
        with pytest.raises(ValidationError):
            OpenAITTSService(api_key="k", voice="bogus")

    def test_compatible_endpoint_keeps_free_form_voice(self):
        cfg = OpenAITTSService(
            api_key="k", base_url="http://kokoro.local/v1", voice="af_bella"
        )
        assert cfg.voice == "af_bella"


class TestCustomVoice:
    def test_object_serialization(self):
        assert openai_voice_param("custom", "marin", "voice_123abc") == {
            "id": "voice_123abc"
        }
        assert openai_voice_param("builtin", "marin", None) == "marin"

    @pytest.mark.parametrize(
        "cls",
        [OpenAILiveLLMConfiguration, OpenAIRealtimeLLMConfiguration, OpenAITTSService],
    )
    def test_custom_requires_wellformed_id(self, cls):
        ok = cls(api_key="k", voice_type="custom", custom_voice_id="voice_123abc")
        assert ok.custom_voice_id == "voice_123abc"
        for bad in (None, "", "marin", "voice_", "voice 123"):
            with pytest.raises(ValidationError):
                cls(api_key="k", voice_type="custom", custom_voice_id=bad)

    def test_custom_voice_id_ignored_for_builtin(self):
        cfg = OpenAILiveLLMConfiguration(api_key="k", custom_voice_id="whatever")
        assert cfg.voice_type == "builtin"

    def test_unknown_voice_type_rejected(self):
        with pytest.raises(ValidationError):
            OpenAILiveLLMConfiguration(api_key="k", voice_type="cloned")


class TestSecretsAndOverrides:
    def test_api_key_masked_for_browser(self):
        secret = "sk-live-secret-abcdefghijkl"
        masked = mask_user_config(
            _effective(OpenAILiveLLMConfiguration(api_key=secret))
        )
        assert masked["realtime"]["api_key"] != secret
        assert secret not in str(masked)
        assert masked["realtime"]["voice"] == "meridian"

    def test_workflow_override_changes_voice(self):
        base = _effective(OpenAILiveLLMConfiguration(api_key="k"))
        resolved = resolve_effective_config(
            base, {"realtime": {"provider": "openai_live", "voice": "gleam"}}
        )
        assert resolved.realtime.voice == "gleam"
        assert base.realtime.voice == "meridian"  # original untouched

    def test_workflow_override_with_invalid_voice_rejected(self):
        base = _effective(OpenAILiveLLMConfiguration(api_key="k"))
        with pytest.raises(ValueError):
            resolve_effective_config(
                base, {"realtime": {"provider": "openai_live", "voice": "not-a-voice"}}
            )

    def test_workflow_override_to_custom_voice(self):
        base = _effective(OpenAIRealtimeLLMConfiguration(api_key="k"))
        resolved = resolve_effective_config(
            base,
            {
                "realtime": {
                    "provider": "openai_realtime",
                    "voice_type": "custom",
                    "custom_voice_id": "voice_abc123",
                }
            },
        )
        assert resolved.realtime.voice_type == "custom"
        assert resolved.realtime.custom_voice_id == "voice_abc123"
