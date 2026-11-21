"""AssemblyAI platform STT: config, factory, adapter behaviour, context.

No network: the WebSocket is never opened; protocol messages are fed straight
into the service's handlers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.services.assemblyai import stt as fork_stt
from websockets.protocol import State

from api.services.configuration import assemblyai as aai
from api.services.configuration.registry import (
    AssemblyAISTTConfiguration,
    ServiceProviders,
)
from api.services.pipecat.assemblyai_context import (
    compose_agent_context,
    infer_expected_reply,
)
from api.services.pipecat.assemblyai_stt import DograhAssemblyAISTTService
from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.service_factory import (
    create_stt_service,
    stt_uses_external_turns,
)

MODEL = "assemblyai-stt-3-7-realtime-agent"
KEY = "aai-platform-test-key-0000"


def _audio(rate: int = 8000) -> AudioConfig:
    return AudioConfig(transport_in_sample_rate=rate, transport_out_sample_rate=rate)


def _user_config(**stt_fields):
    return SimpleNamespace(
        stt=AssemblyAISTTConfiguration(
            provider=ServiceProviders.ASSEMBLYAI, **stt_fields
        )
    )


def _query(service) -> dict:
    # sample_rate is resolved on StartFrame; mirror that without a pipeline.
    service._sample_rate = service._init_sample_rate
    return {
        k: v[0] for k, v in parse_qs(urlsplit(service._build_ws_url()).query).items()
    }


@pytest.fixture
def platform_key(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", KEY)
    return KEY


# --------------------------------------------------------------- config


def test_config_defaults_to_voice_agent_model_without_key():
    config = AssemblyAISTTConfiguration(provider=ServiceProviders.ASSEMBLYAI)
    assert config.model == MODEL
    assert config.api_key is None
    assert config.mode == "balanced"
    assert config.voice_focus == "near-field"
    assert config.turn_detection == "assemblyai"
    assert config.encoding == "pcm_s16le"


def test_saved_legacy_configs_still_validate():
    legacy = AssemblyAISTTConfiguration.model_validate(
        {
            "provider": "assemblyai",
            "api_key": "k",
            "model": "u3-rt-pro",
            "language": "de",
        }
    )
    assert legacy.model == "u3-rt-pro"
    assert legacy.get_all_api_keys() == ["k"]


def test_schema_lists_new_model_first_for_the_ui():
    schema = AssemblyAISTTConfiguration.model_json_schema()["properties"]
    assert schema["model"]["examples"][0] == MODEL
    assert schema["mode"]["enum"] == ["balanced", "min_latency", "max_accuracy"]
    assert schema["agent_context"]["type"] == "boolean"


def test_default_provider_is_assemblyai_with_env_rollback(monkeypatch):
    from api.services.configuration import defaults

    monkeypatch.delenv("STT_PLATFORM_DEFAULT_PROVIDER", raising=False)
    assert defaults._default_stt_provider() == ServiceProviders.ASSEMBLYAI
    monkeypatch.setenv("STT_PLATFORM_DEFAULT_PROVIDER", "deepgram")
    assert defaults._default_stt_provider() == ServiceProviders.DEEPGRAM
    monkeypatch.setenv("STT_PLATFORM_DEFAULT_PROVIDER", "nonsense")
    assert defaults._default_stt_provider() == ServiceProviders.DEEPGRAM


def test_byok_key_wins_over_platform_key(platform_key):
    byok = AssemblyAISTTConfiguration(provider="assemblyai", api_key="tenant-key")
    assert aai.resolve_assemblyai_api_key(byok) == ("tenant-key", "byok")
    keyless = AssemblyAISTTConfiguration(provider="assemblyai")
    assert aai.resolve_assemblyai_api_key(keyless) == (KEY, "platform")


def test_missing_credential_is_reported(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    keyless = AssemblyAISTTConfiguration(provider="assemblyai")
    assert aai.resolve_assemblyai_api_key(keyless) == (None, "missing")
    with pytest.raises(HTTPException) as exc:
        create_stt_service(_user_config(), _audio())
    assert exc.value.status_code == 503
    assert "ASSEMBLYAI_API_KEY" in exc.value.detail


def test_validity_check_requires_platform_key_for_keyless_config(monkeypatch):
    from api.services.configuration.check_validity import UserConfigurationValidator

    validator = UserConfigurationValidator.__new__(UserConfigurationValidator)
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ASSEMBLYAI_API_KEY"):
        validator._check_assemblyai_api_key("assemblyai", None)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", KEY)
    assert validator._check_assemblyai_api_key("assemblyai", None)
    assert validator._check_assemblyai_api_key("assemblyai", "tenant")


def test_language_codes_and_keyterm_limits():
    assert aai.language_codes_for("en") == ["en"]
    assert aai.language_codes_for("es") == ["es"]
    assert aai.language_codes_for("multi") == ["en", "es"]
    assert aai.language_codes_for(None) == ["en", "es"]
    terms = ["Medicare", "medicare", "x" * 51, "  Final   Expense "] + [
        f"term{i}" for i in range(200)
    ]
    cleaned = aai.sanitize_keyterms(terms)
    assert cleaned[:2] == ["Medicare", "Final Expense"]
    assert len(cleaned) == 100
    assert all(len(t) <= 50 for t in cleaned)
    assert aai.sanitize_keyterms([]) is None


def test_cost_estimate_and_budget_defaults(monkeypatch):
    for name in (
        "ASSEMBLYAI_STT_PRICE_PER_HOUR",
        "ASSEMBLYAI_DAILY_BUDGET_USD",
        "ASSEMBLYAI_TOTAL_BUDGET_USD",
        "ASSEMBLYAI_MAX_CONCURRENT_STREAMS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert aai.estimate_cost_usd(3600, voice_focus=False) == pytest.approx(0.45)
    assert aai.estimate_cost_usd(3600, voice_focus=True) == pytest.approx(0.55)
    budgets = aai.budget_settings()
    assert (budgets.daily_budget_usd, budgets.total_budget_usd) == (5.0, 25.0)
    assert budgets.max_concurrent_streams == 5


# -------------------------------------------------------------- factory


def test_factory_builds_v3_session_for_8khz_phone_audio(platform_key):
    service = create_stt_service(
        _user_config(language="multi"),
        _audio(8000),
        keyterms=["Final Expense", "beneficiary"],
    )
    assert isinstance(service, DograhAssemblyAISTTService)
    url = service._build_ws_url()
    assert url.startswith("wss://streaming.assemblyai.com/v3/ws?")
    assert KEY not in url  # the key goes in the Authorization header only
    q = _query(service)
    assert q["speech_model"] == MODEL
    assert q["encoding"] == "pcm_s16le"  # pipeline audio is linear PCM
    assert q["sample_rate"] == "8000"  # no resampling of phone audio
    assert json.loads(q["language_codes"]) == ["en", "es"]
    assert "language_code" not in q
    assert q["mode"] == "balanced"
    assert q["voice_focus"] == "near-field"
    assert json.loads(q["keyterms_prompt"]) == ["Final Expense", "beneficiary"]
    assert q["language_detection"] == "true"
    # AssemblyAI owns turn ends; local min/max silence overrides are not sent.
    assert "max_turn_silence" not in q
    assert service._vad_force_turn_endpoint is False
    assert service._should_interrupt is False
    assert stt_uses_external_turns(_user_config()) is True


def test_factory_pipecat_turn_mode_pins_fast_finals(platform_key):
    service = create_stt_service(
        _user_config(turn_detection="pipecat", voice_focus="off", keyterms=False),
        _audio(16000),
        keyterms=["ignored"],
    )
    q = _query(service)
    assert q["sample_rate"] == "16000"
    assert q["min_turn_silence"] == q["max_turn_silence"] == "100"
    assert "mode" not in q and "voice_focus" not in q and "keyterms_prompt" not in q
    assert service._vad_force_turn_endpoint is True
    assert stt_uses_external_turns(_user_config(turn_detection="pipecat")) is False


def test_new_models_get_the_voice_agent_feature_set():
    assert fork_stt.is_u3_pro_model(MODEL)
    assert fork_stt.is_u3_pro_model("universal-3-6-pro")
    assert fork_stt.is_u3_pro_model("u3-rt-pro")
    # The notetaker model has no Voice Focus / agent features.
    assert not fork_stt.is_u3_pro_model("assemblyai-stt-3-7-realtime")


# -------------------------------------------------------------- adapter


class _Usage:
    def __init__(self):
        self.billed = 0.0
        self.errors = []
        self.opened = 0
        self.finished = 0

    def connection_opened(self):
        self.opened += 1

    def add_billed_seconds(self, s):
        self.billed += s

    def record_error(self, code):
        self.errors.append(code)

    async def finish(self):
        self.finished += 1


def _service(**kwargs) -> DograhAssemblyAISTTService:
    svc = DograhAssemblyAISTTService(
        api_key=KEY,
        settings=DograhAssemblyAISTTService.Settings(model=MODEL),
        sample_rate=8000,
        vad_force_turn_endpoint=False,
        should_interrupt=False,
        **kwargs,
    )
    svc.push_frame = AsyncMock()
    svc.broadcast_frame = AsyncMock()
    svc.push_error = AsyncMock()
    svc.stop_processing_metrics = AsyncMock()
    svc._trace_transcription = AsyncMock()
    svc._call_event_handler = AsyncMock()
    return svc


def _turn(order: int, text: str, *, final: bool) -> dict:
    return {
        "type": "Turn",
        "turn_order": order,
        "turn_is_formatted": final,
        "end_of_turn": final,
        "transcript": text,
        "end_of_turn_confidence": 0.9 if final else 0.1,
        "words": [],
    }


def _pushed(svc, kind):
    return [
        c.args[0] for c in svc.push_frame.await_args_list if isinstance(c.args[0], kind)
    ]


async def test_one_final_per_turn_and_no_repeated_partials():
    svc = _service()
    for msg in (
        _turn(0, "yes", final=False),
        _turn(0, "yes", final=False),  # identical partial: not re-emitted
        _turn(0, "yes I am", final=False),
        _turn(0, "Yes, I am.", final=True),
        _turn(0, "Yes, I am.", final=True),  # duplicate final: dropped
        _turn(0, "yes I am", final=False),  # late partial of a final turn: dropped
        _turn(1, "Texas.", final=True),
    ):
        await svc._handle_message(msg)
    interims = [f.text for f in _pushed(svc, InterimTranscriptionFrame)]
    finals = [
        f.text
        for f in _pushed(svc, TranscriptionFrame)
        if not isinstance(f, InterimTranscriptionFrame)
    ]
    assert interims == ["yes", "yes I am"]
    assert finals == ["Yes, I am.", "Texas."]


async def test_partial_yes_is_never_a_final_transcript():
    """Only finals reach the LLM; a partial 'yes' cannot trigger a transfer."""
    svc = _service()
    await svc._handle_message(_turn(0, "yes", final=False))
    assert not [
        f
        for f in _pushed(svc, TranscriptionFrame)
        if not isinstance(f, InterimTranscriptionFrame)
    ]


async def test_empty_force_endpoint_final_is_ignored():
    svc = _service()
    await svc._handle_message(_turn(3, "", final=True))
    svc.push_frame.assert_not_awaited()


async def test_auth_error_stops_reconnects_and_is_metered():
    usage = _Usage()
    svc = _service(usage=usage)
    await svc._handle_message(
        {"type": "Error", "error_code": 1008, "error": "Invalid API key"}
    )
    assert svc._reconnect_on_error is False
    assert usage.errors == [1008]
    svc.push_error.assert_awaited()
    assert KEY not in str(svc.push_error.await_args)


async def test_concurrency_limit_error_stays_retryable():
    svc = _service()
    await svc._handle_message(
        {"type": "Error", "error_code": 1008, "error": "Too many concurrent sessions"}
    )
    assert svc._reconnect_on_error is True


async def test_informational_messages_are_ignored():
    svc = _service()
    await svc._handle_message({"type": "Heartbeat", "total_audio_received_ms": 5000})
    svc.push_error.assert_not_awaited()


async def test_begin_reports_served_model_and_termination_bills_session():
    usage = _Usage()
    svc = _service(usage=usage)
    svc._connection_started_at = 1.0
    await svc._handle_message(
        {
            "type": "Begin",
            "id": "s1",
            "expires_at": 0,
            "configuration": {"model": "universal-3-6-pro"},
        }
    )
    assert svc.begin_configuration == {"model": "universal-3-6-pro"}
    await svc._handle_message(
        {
            "type": "Termination",
            "audio_duration_seconds": 9.0,
            "session_duration_seconds": 12.5,
        }
    )
    assert usage.billed == pytest.approx(12.5)
    svc._account_unterminated_connection()  # already billed: nothing more
    assert usage.billed == pytest.approx(12.5)


async def test_offline_audio_is_bounded_and_not_replayed():
    svc = _service(max_offline_buffer_ms=500)
    svc._sample_rate = 8000
    svc._websocket = None
    for _ in range(20):  # 2 s of 100 ms frames while disconnected
        async for _frame in svc.run_stt(b"\x01\x00" * 800):
            pass
    assert len(svc._audio_buffer) == 8000  # newest 500 ms at 8 kHz PCM16
    assert svc._dropped_offline_bytes == 32000 - 8000


async def test_mulaw_chunks_are_sized_per_byte():
    svc = _service(encoding="pcm_mulaw")
    svc._sample_rate = 8000
    assert svc._bytes_for_ms(50) == 400
    pcm = _service()
    pcm._sample_rate = 8000
    assert pcm._bytes_for_ms(50) == 800


async def test_assistant_turn_updates_agent_context_live():
    svc = _service(language_hint="en")
    svc.set_node_name_provider(lambda: "Confirm State")
    ws = SimpleNamespace(state=State.OPEN, send=AsyncMock())
    svc._websocket = ws
    await svc._process_assistant_turn("Thanks, John. You're in Texas, correct?")
    sent = json.loads(ws.send.await_args.args[0])
    assert sent["type"] == "UpdateConfiguration"
    assert "You're in Texas, correct?" in sent["agent_context"]
    assert "step: Confirm State" in sent["agent_context"]
    assert "US state" in sent["agent_context"]


async def test_agent_context_can_be_disabled():
    svc = _service(agent_context_enabled=False)
    ws = SimpleNamespace(state=State.OPEN, send=AsyncMock())
    svc._websocket = ws
    await svc._process_assistant_turn("How old are you?")
    ws.send.assert_not_awaited()


# -------------------------------------------------------------- context


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How old are you?", "age"),
        ("¿Cuántos años tiene?", "age"),
        ("You're in Texas, correct?", "US state"),
        ("What state do you live in?", "US state"),
        ("Do you have a few minutes to complete the application?", "yes/no"),
        ("Could you spell your last name for me?", "name"),
        ("Great.", None),
    ],
)
def test_expected_reply_types(question, expected):
    hint = infer_expected_reply(question)
    if expected is None:
        assert hint is None
    else:
        assert expected in hint


def test_context_never_suggests_an_answer():
    for question in (
        "Do you have a few minutes?",
        "Would you like me to transfer you to a licensed agent?",
        "You're in Texas, correct?",
    ):
        text = compose_agent_context(question, node_name="Qualify", language="en")
        hint = text.split("\n", 1)[1]
        # Answer *types* only: "yes" may appear solely inside "yes/no".
        assert hint.replace("yes/no", "").lower().count("yes") == 0
        assert "consent" not in hint.lower()


def test_context_is_clipped_and_plain_without_hints():
    long = "word " * 2000 + "How old are you?"
    composed = compose_agent_context(long, node_name="Age", language="es")
    assert len(composed) <= aai.ASSEMBLYAI_MAX_AGENT_CONTEXT_CHARS
    assert composed.rstrip().endswith("language: Spanish.)")
    assert (
        compose_agent_context("How old are you?", include_hints=False)
        == "How old are you?"
    )
    assert compose_agent_context("   ") == ""


async def test_simultaneous_sessions_keep_independent_turn_state():
    """Two calls on one worker: one call's turns never leak into the other."""
    usage_a, usage_b = _Usage(), _Usage()
    a, b = _service(usage=usage_a), _service(usage=usage_b)
    await a._handle_message(_turn(0, "Seventy four.", final=True))
    await b._handle_message(_turn(0, "No.", final=False))
    await b._handle_message(_turn(0, "No.", final=True))
    await a._handle_message({"type": "Error", "error_code": 3005, "error": "x"})
    finals_a = [
        f.text
        for f in _pushed(a, TranscriptionFrame)
        if not isinstance(f, InterimTranscriptionFrame)
    ]
    finals_b = [
        f.text
        for f in _pushed(b, TranscriptionFrame)
        if not isinstance(f, InterimTranscriptionFrame)
    ]
    assert finals_a == ["Seventy four."] and finals_b == ["No."]
    assert usage_a.errors == [3005] and usage_b.errors == []
    assert b._reconnect_on_error is True


def test_eval_metrics_wer_and_endpoints():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from evals.stt.metrics import endpoint_errors, word_error_rate

    assert word_error_rate("Yes, Texas.", "yes texas") == 0.0
    assert word_error_rate("seventy-four", "seventy four") == 0.0
    assert word_error_rate("tengo setenta años", "tengo setenta anos") == 0.0
    assert word_error_rate("I'm eighty one", "I'm eighty") == pytest.approx(
        1 / 3, abs=1e-3
    )
    assert word_error_rate("", "") is None
    assert word_error_rate("", "hello") == 1.0
    assert endpoint_errors(1, 3) == {"false_endpoints": 2, "missed_endpoints": 0}
    assert endpoint_errors(2, 1) == {"false_endpoints": 0, "missed_endpoints": 1}
