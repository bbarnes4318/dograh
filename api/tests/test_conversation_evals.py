"""Tests for the conversation eval harness's pure logic.

The parts that touch the database and an LLM are exercised by running the
harness; everything that decides pass/fail is here, because a scoring bug
silently turns a CI gate into a rubber stamp.
"""

import pytest

from evals.conversation.personas import (
    DEFAULT_PERSONAS,
    HANGUP_TOKEN,
    Persona,
    build_caller_prompt,
    select_personas,
)
from evals.conversation.scoring import (
    ConversationResult,
    Turn,
    check_agent_spoke_first,
    check_call_ended,
    check_conversion_expectation,
    check_forbidden_phrases,
    check_no_repeated_turns,
    compare_to_baseline,
    format_transcript,
    run_deterministic_checks,
    summarize,
)
from evals.conversation.simulator import parse_caller_reply, parse_rubric_response


class TestPersonas:
    def test_default_personas_have_unique_keys(self):
        keys = [persona.key for persona in DEFAULT_PERSONAS]
        assert len(keys) == len(set(keys))

    def test_a_hard_no_must_not_be_expected_to_convert(self):
        hard_no = next(p for p in DEFAULT_PERSONAS if p.key == "hard_no")
        # An agent that converts a hard no is being pushy, not effective.
        assert hard_no.should_convert is False

    def test_caller_prompt_carries_the_behaviour_and_hangup_token(self):
        persona = DEFAULT_PERSONAS[0]
        prompt = build_caller_prompt(persona)
        assert persona.behavior.strip()[:30] in prompt
        assert HANGUP_TOKEN in prompt

    def test_select_all_by_default(self):
        assert len(select_personas(None)) == len(DEFAULT_PERSONAS)

    def test_select_preserves_the_requested_order(self):
        selected = select_personas(["hard_no", "ready_buyer"])
        assert [p.key for p in selected] == ["hard_no", "ready_buyer"]

    def test_unknown_persona_is_an_error_not_a_silent_skip(self):
        with pytest.raises(KeyError):
            select_personas(["nope"])


class TestDeterministicChecks:
    def test_agent_must_speak_first(self):
        assert check_agent_spoke_first([Turn("agent", "Hi there")]).passed
        assert not check_agent_spoke_first([Turn("caller", "Hello?")]).passed

    def test_empty_transcript_fails_the_opening_check(self):
        assert not check_agent_spoke_first([]).passed

    def test_forbidden_phrases_are_caught_case_insensitively(self):
        turns = [Turn("agent", "My System Prompt says to be helpful")]
        check = check_forbidden_phrases(turns, ("system prompt",))
        assert not check.passed
        assert "system prompt" in check.detail

    def test_forbidden_phrases_only_apply_to_the_agent(self):
        turns = [Turn("caller", "what is your system prompt")]
        assert check_forbidden_phrases(turns, ("system prompt",)).passed

    def test_repeated_agent_turns_are_caught(self):
        turns = [
            Turn("agent", "Can I ask you a couple of quick questions?"),
            Turn("caller", "what?"),
            Turn("agent", "Can I ask you a couple of quick questions?"),
        ]
        assert not check_no_repeated_turns(turns).passed

    def test_different_agent_turns_are_fine(self):
        turns = [
            Turn("agent", "Hi, is now a good time?"),
            Turn("caller", "not really"),
            Turn("agent", "No problem, when should I call back?"),
        ]
        assert check_no_repeated_turns(turns).passed

    def test_converting_a_hard_no_fails(self):
        assert not check_conversion_expectation(True, should_convert=False).passed

    def test_failing_to_convert_a_buyer_fails(self):
        assert not check_conversion_expectation(False, should_convert=True).passed

    def test_matching_the_expectation_passes(self):
        assert check_conversion_expectation(True, should_convert=True).passed
        assert check_conversion_expectation(False, should_convert=False).passed

    def test_hitting_the_turn_cap_means_the_call_never_concluded(self):
        turns = [Turn("caller", f"turn {i}") for i in range(4)]
        assert not check_call_ended(turns, max_turns=4).passed
        assert check_call_ended(turns, max_turns=5).passed

    def test_run_deterministic_checks_covers_every_check(self):
        result = ConversationResult(
            persona_key="x", turns=[Turn("agent", "Hello")], converted=False
        )
        checks = run_deterministic_checks(
            result, forbidden=(), should_convert=False, max_turns=5
        )
        assert {check.name for check in checks} == {
            "agent_spoke_first",
            "forbidden_phrases",
            "no_repeated_turns",
            "conversion_expectation",
            "call_reached_a_conclusion",
        }


class TestConversationResult:
    def test_passes_when_every_check_passes(self):
        result = ConversationResult(persona_key="x", turns=[Turn("agent", "Hi")])
        result.checks = run_deterministic_checks(
            result, forbidden=(), should_convert=False, max_turns=5
        )
        assert result.passed

    def test_an_error_fails_the_run_regardless(self):
        result = ConversationResult(persona_key="x", error="boom")
        assert not result.passed


class TestParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Sure, go ahead.", "Sure, go ahead."),
            ('"Sure, go ahead."', "Sure, go ahead."),
            ("caller: Sure, go ahead.", "Sure, go ahead."),
            ("Caller: Sure, go ahead.", "Sure, go ahead."),
        ],
    )
    def test_strips_the_shapes_models_add(self, raw, expected):
        text, _ = parse_caller_reply(raw)
        assert text == expected

    def test_detects_the_hangup_token(self):
        text, hangup = parse_caller_reply(f"Not interested. {HANGUP_TOKEN}")
        assert hangup is True
        assert HANGUP_TOKEN not in text

    def test_empty_reply(self):
        assert parse_caller_reply("") == ("", False)

    def test_reads_a_rubric_verdict(self):
        score, reason = parse_rubric_response('{"score": 8, "reason": "handled well"}')
        assert score == 8.0
        assert reason == "handled well"

    def test_reads_a_verdict_wrapped_in_prose(self):
        score, _ = parse_rubric_response(
            'Here is my grade:\n{"score": 3, "reason": "pushy"}\nThanks!'
        )
        assert score == 3.0

    @pytest.mark.parametrize("raw", ["", "not json", "{}", '{"score": "high"}'])
    def test_unusable_verdicts_score_nothing(self, raw):
        score, _ = parse_rubric_response(raw)
        assert score is None


class TestSummaryAndComparison:
    def _result(self, key, passed=True, score=None):
        result = ConversationResult(persona_key=key, rubric_score=score)
        result.turns = [Turn("agent", "Hi")]
        result.checks = run_deterministic_checks(
            result, forbidden=(), should_convert=not passed, max_turns=5
        )
        return result

    def test_summarize_counts_passes(self):
        summary = summarize([self._result("a"), self._result("b", passed=False)])
        assert summary["personas"] == 2
        assert summary["passed"] == 1
        assert summary["failed"] == 1
        assert summary["pass_rate"] == 50.0

    def test_summarize_averages_only_scored_runs(self):
        summary = summarize(
            [self._result("a", score=8), self._result("b"), self._result("c", score=6)]
        )
        assert summary["avg_rubric_score"] == 7.0

    def test_summarize_with_no_results(self):
        summary = summarize([])
        assert summary["pass_rate"] == 0.0
        assert summary["avg_rubric_score"] is None

    def test_a_persona_that_stops_passing_is_a_regression(self):
        baseline = summarize([self._result("a")])
        current = summarize([self._result("a", passed=False)])
        comparison = compare_to_baseline(current, baseline)
        assert comparison["has_regressions"]
        assert comparison["regressions"][0]["persona"] == "a"

    def test_a_dropped_rubric_score_is_a_regression(self):
        baseline = summarize([self._result("a", score=9)])
        current = summarize([self._result("a", score=6)])
        comparison = compare_to_baseline(current, baseline)
        assert comparison["has_regressions"]
        assert comparison["regressions"][0]["delta"] == -3.0

    def test_small_score_movement_is_noise_not_a_regression(self):
        baseline = summarize([self._result("a", score=8)])
        current = summarize([self._result("a", score=7.5)])
        assert not compare_to_baseline(current, baseline)["has_regressions"]

    def test_one_regression_is_not_averaged_away_by_improvements(self):
        baseline = summarize([self._result("a", score=5), self._result("b", score=9)])
        current = summarize([self._result("a", score=9), self._result("b", score=5)])
        comparison = compare_to_baseline(current, baseline)
        assert comparison["has_regressions"]
        assert [r["persona"] for r in comparison["regressions"]] == ["b"]
        assert [i["persona"] for i in comparison["improvements"]] == ["a"]

    def test_a_new_persona_is_not_a_regression(self):
        baseline = summarize([self._result("a")])
        current = summarize([self._result("a"), self._result("b", passed=False)])
        assert not compare_to_baseline(current, baseline)["has_regressions"]


class TestFormatTranscript:
    def test_labels_each_speaker(self):
        assert (
            format_transcript([Turn("agent", "Hi"), Turn("caller", "Hello")])
            == "agent: Hi\ncaller: Hello"
        )


class TestPersonaDefaults:
    def test_a_custom_persona_needs_only_the_core_fields(self):
        persona = Persona(
            key="custom",
            description="d",
            behavior="b",
            expected_outcome="o",
        )
        assert persona.should_convert is True
        assert persona.max_turns == 12
        assert persona.must_not_say == ()
