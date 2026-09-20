"""Tests for covering node-transition latency and prompt-cache routing.

Moving between nodes costs two model round trips — one that picks the
transition, one that speaks in the new node — and the gap between them is
silence. The transition function can carry a line for the agent to say,
returned by the *first* call, so it costs no extra round trip.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.workflow.pipecat_engine import (
    MAX_TRANSITION_MESSAGE_CHARS,
    PROMPT_CACHE_KEY_PROVIDERS,
    PipecatEngine,
    _clean_transition_message,
)
from api.services.workflow.pipecat_engine_context_composer import (
    TRANSITION_MESSAGE_ARG,
    compose_functions_for_node,
)


def _edge(name: str, condition: str, transition_speech=None):
    edge = MagicMock()
    edge.get_function_name.return_value = name
    edge.condition = condition
    edge.transition_speech = transition_speech
    return edge


def _node(out_edges):
    return SimpleNamespace(
        out_edges=out_edges,
        document_uuids=None,
        tool_uuids=None,
        mcp_tool_filters=None,
    )


class TestTransitionMessageSchema:
    async def test_absent_when_disabled(self):
        functions = await compose_functions_for_node(
            node=_node([_edge("to_qualify", "user is ready")]),
            custom_tool_manager=None,
            include_transition_message=False,
        )
        assert functions[0].properties == {}

    async def test_offered_when_enabled(self):
        functions = await compose_functions_for_node(
            node=_node([_edge("to_qualify", "user is ready")]),
            custom_tool_manager=None,
            include_transition_message=True,
        )
        assert TRANSITION_MESSAGE_ARG in functions[0].properties
        # Optional: the model may transition without saying anything.
        assert functions[0].required == []

    async def test_not_offered_when_the_edge_already_has_a_line(self):
        functions = await compose_functions_for_node(
            node=_node(
                [_edge("to_qualify", "ready", transition_speech="Great, one sec.")]
            ),
            custom_tool_manager=None,
            include_transition_message=True,
        )
        # Would otherwise stack two lines back to back.
        assert functions[0].properties == {}

    async def test_mixed_edges_are_handled_independently(self):
        functions = await compose_functions_for_node(
            node=_node(
                [
                    _edge("a", "cond a", transition_speech="configured"),
                    _edge("b", "cond b"),
                ]
            ),
            custom_tool_manager=None,
            include_transition_message=True,
        )
        assert functions[0].properties == {}
        assert TRANSITION_MESSAGE_ARG in functions[1].properties


class TestCleanTransitionMessage:
    @pytest.mark.parametrize("value", [None, "", "   ", 42, {"a": 1}, []])
    def test_rejects_unusable_values(self, value):
        assert _clean_transition_message(value) is None

    def test_trims_whitespace(self):
        assert _clean_transition_message("  got it, one sec  ") == "got it, one sec"

    def test_drops_an_overlong_line(self):
        # A paragraph would delay the very generation the line exists to cover.
        assert _clean_transition_message("x" * (MAX_TRANSITION_MESSAGE_CHARS + 1)) is None

    def test_keeps_a_line_at_the_limit(self):
        text = "x" * MAX_TRANSITION_MESSAGE_CHARS
        assert _clean_transition_message(text) == text


def _engine(provider=None, namespace=None, extra=None):
    engine = PipecatEngine.__new__(PipecatEngine)
    engine._llm_provider = provider
    engine._prompt_cache_namespace = namespace
    engine.llm = SimpleNamespace(
        _settings=SimpleNamespace(extra={} if extra is None else extra)
    )
    return engine


class TestPromptCacheKey:
    def test_sets_a_per_node_key_for_a_supported_provider(self):
        engine = _engine(provider="openai", namespace="dograh:1:7")

        engine._apply_prompt_cache_key("node-3")

        assert engine.llm._settings.extra["prompt_cache_key"] == "dograh:1:7:node-3"

    def test_key_changes_with_the_node(self):
        engine = _engine(provider="openai", namespace="dograh:1:7")

        engine._apply_prompt_cache_key("node-3")
        first = engine.llm._settings.extra["prompt_cache_key"]
        engine._apply_prompt_cache_key("node-4")

        assert engine.llm._settings.extra["prompt_cache_key"] != first

    @pytest.mark.parametrize("provider", ["groq", "openrouter", "speaches", None])
    def test_skips_providers_that_would_reject_the_parameter(self, provider):
        engine = _engine(provider=provider, namespace="dograh:1:7")

        engine._apply_prompt_cache_key("node-3")

        assert "prompt_cache_key" not in engine.llm._settings.extra

    def test_skips_when_there_is_no_namespace(self):
        engine = _engine(provider="openai", namespace=None)

        engine._apply_prompt_cache_key("node-3")

        assert "prompt_cache_key" not in engine.llm._settings.extra

    def test_tolerates_a_service_without_extra_settings(self):
        engine = _engine(provider="openai", namespace="dograh:1:7")
        engine.llm = SimpleNamespace(_settings=SimpleNamespace())

        # Must not raise — a provider without `extra` simply opts out.
        engine._apply_prompt_cache_key("node-3")

    def test_allowlist_is_restrictive(self):
        assert PROMPT_CACHE_KEY_PROVIDERS == frozenset({"openai", "azure"})


class TestTransitionSpeaksTheModelsLine:
    async def _run_transition(self, *, arguments, transition_speech=None):
        engine = PipecatEngine.__new__(PipecatEngine)
        engine.task = MagicMock()
        engine.task.queue_frame = AsyncMock()
        engine._queued_speech_mute_state = "idle"
        engine._transition_in_progress = False
        engine._fetch_recording_audio = None
        engine._audio_config = None
        engine._current_node = SimpleNamespace(is_end=False)
        engine._perform_variable_extraction_if_needed = AsyncMock()
        engine.set_node = AsyncMock()
        engine.end_call_with_reason = AsyncMock()

        func = await PipecatEngine._create_transition_func(
            engine, "to_qualify", "node-2", transition_speech, None, None
        )

        params = SimpleNamespace(
            arguments=arguments, result_callback=AsyncMock()
        )
        await func(params)
        return engine

    def _spoken(self, engine) -> list[str]:
        return [
            call.args[0].text
            for call in engine.task.queue_frame.call_args_list
            if hasattr(call.args[0], "text")
        ]

    async def test_speaks_the_line_the_model_supplied(self):
        engine = await self._run_transition(
            arguments={TRANSITION_MESSAGE_ARG: "Got it, one sec."}
        )
        assert self._spoken(engine) == ["Got it, one sec."]

    async def test_configured_speech_wins_over_the_models_line(self):
        engine = await self._run_transition(
            arguments={TRANSITION_MESSAGE_ARG: "Got it, one sec."},
            transition_speech="Configured line.",
        )
        assert self._spoken(engine) == ["Configured line."]

    async def test_says_nothing_when_the_model_offered_nothing(self):
        engine = await self._run_transition(arguments={})
        assert self._spoken(engine) == []

    async def test_ignores_an_overlong_line(self):
        engine = await self._run_transition(
            arguments={TRANSITION_MESSAGE_ARG: "x" * 500}
        )
        assert self._spoken(engine) == []

    async def test_marks_the_transition_in_flight(self):
        engine = await self._run_transition(
            arguments={TRANSITION_MESSAGE_ARG: "one sec"}
        )
        # Still held: the generation this transition queues hasn't started, so
        # anything else touching the context should ride it rather than race.
        assert engine._transition_in_progress is True
