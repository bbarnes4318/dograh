"""Scoring a simulated call, and comparing a run against a baseline.

Two layers, deliberately:

**Deterministic checks** need no model and never flake — did the agent say
something it must never say, did it converge or loop, did the call end the way
this persona's outcome requires. These alone catch most prompt regressions.

**A rubric judge** answers the part that needs reading: did the agent actually
do what this persona needed. It is optional, so the harness still runs — and
still fails on the deterministic checks — without an LLM key.

``compare_to_baseline`` is what a CI gate calls: it reports which personas got
worse, so a prompt change that lifts three cohorts and tanks a fourth is
visible rather than averaged away.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence

# A turn that repeats a previous one nearly verbatim means the agent is stuck.
REPEAT_SIMILARITY_THRESHOLD = 0.9


@dataclass
class Turn:
    """One exchange in a simulated call."""

    speaker: str  # "agent" or "caller"
    text: str


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ConversationResult:
    """The outcome of running one persona against one workflow."""

    persona_key: str
    turns: list[Turn] = field(default_factory=list)
    disposition: str = ""
    converted: bool = False
    node_path: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    rubric_score: Optional[float] = None
    rubric_reason: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        # `passed` is a property, so asdict() leaves it out — and a saved
        # baseline without it makes every regression comparison a no-op.
        return {**asdict(self), "passed": self.passed}


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _similarity(left: str, right: str) -> float:
    """Word-overlap ratio, enough to spot a near-verbatim repeat."""
    left_words = _normalize(left).split()
    right_words = _normalize(right).split()
    if not left_words or not right_words:
        return 0.0
    shared = len(set(left_words) & set(right_words))
    return shared / max(len(set(left_words)), len(set(right_words)))


def check_forbidden_phrases(
    turns: Sequence[Turn], forbidden: Sequence[str]
) -> CheckResult:
    """The agent must never say certain things, whatever the caller asks."""
    agent_text = _normalize(
        " ".join(turn.text for turn in turns if turn.speaker == "agent")
    )
    hits = [phrase for phrase in forbidden if _normalize(phrase) in agent_text]
    return CheckResult(
        name="forbidden_phrases",
        passed=not hits,
        detail=f"said: {', '.join(hits)}" if hits else "",
    )


def check_no_repeated_turns(turns: Sequence[Turn]) -> CheckResult:
    """An agent repeating itself is stuck, and callers hang up on it."""
    agent_turns = [turn.text for turn in turns if turn.speaker == "agent"]
    for index in range(1, len(agent_turns)):
        for previous in agent_turns[max(0, index - 2) : index]:
            if _similarity(agent_turns[index], previous) >= REPEAT_SIMILARITY_THRESHOLD:
                return CheckResult(
                    name="no_repeated_turns",
                    passed=False,
                    detail=f"turn {index + 1} repeats an earlier one",
                )
    return CheckResult(name="no_repeated_turns", passed=True)


def check_agent_spoke_first(turns: Sequence[Turn]) -> CheckResult:
    """An outbound call where the agent says nothing first is dead air."""
    first = next((turn for turn in turns if turn.text.strip()), None)
    return CheckResult(
        name="agent_spoke_first",
        passed=bool(first and first.speaker == "agent"),
        detail="" if first and first.speaker == "agent" else "agent opened silent",
    )


def check_conversion_expectation(converted: bool, should_convert: bool) -> CheckResult:
    """Converting a hard no is a failure, not a win."""
    return CheckResult(
        name="conversion_expectation",
        passed=converted == should_convert,
        detail=(
            ""
            if converted == should_convert
            else f"expected converted={should_convert}, got {converted}"
        ),
    )


def check_call_ended(turns: Sequence[Turn], max_turns: int) -> CheckResult:
    """A call that runs to the turn cap never reached a conclusion."""
    caller_turns = sum(1 for turn in turns if turn.speaker == "caller")
    return CheckResult(
        name="call_reached_a_conclusion",
        passed=caller_turns < max_turns,
        detail="" if caller_turns < max_turns else f"hit the {max_turns}-turn cap",
    )


def run_deterministic_checks(
    result: ConversationResult,
    *,
    forbidden: Sequence[str],
    should_convert: bool,
    max_turns: int,
) -> list[CheckResult]:
    """Every check that needs no model, so it never flakes."""
    return [
        check_agent_spoke_first(result.turns),
        check_forbidden_phrases(result.turns, forbidden),
        check_no_repeated_turns(result.turns),
        check_conversion_expectation(result.converted, should_convert),
        check_call_ended(result.turns, max_turns),
    ]


def format_transcript(turns: Iterable[Turn]) -> str:
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)


RUBRIC_SYSTEM_PROMPT = """\
You are grading a recorded phone call between an AI agent and a person.

The person was playing this role:
{description}

A good call, for this person, looks like:
{expected_outcome}

Grade only the agent. Reply with JSON and nothing else:
{{"score": <0-10>, "reason": "<one sentence>"}}

Score 0-3 if the agent failed the expected outcome, 4-6 if it partly got
there, 7-10 if it handled the call well.
"""


def build_rubric_prompt(description: str, expected_outcome: str) -> str:
    return RUBRIC_SYSTEM_PROMPT.format(
        description=description.strip(), expected_outcome=expected_outcome.strip()
    )


def summarize(results: Sequence[ConversationResult]) -> dict[str, Any]:
    """Scorecard across personas."""
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    scored = [r.rubric_score for r in results if r.rubric_score is not None]
    return {
        "personas": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total * 100, 2) if total else 0.0,
        "avg_rubric_score": (round(sum(scored) / len(scored), 2) if scored else None),
        "results": {result.persona_key: result.to_dict() for result in results},
    }


def compare_to_baseline(
    summary: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Which personas got worse than the baseline run.

    Reported per persona rather than as an average, so a change that lifts
    three personas and breaks a fourth still shows up as a regression.
    """
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []

    baseline_results = baseline.get("results", {}) or {}
    for key, current in (summary.get("results", {}) or {}).items():
        before = baseline_results.get(key)
        if before is None:
            continue
        if before.get("passed") and not current.get("passed"):
            regressions.append({"persona": key, "reason": "passed before, fails now"})
            continue
        if not before.get("passed") and current.get("passed"):
            improvements.append({"persona": key, "reason": "fails before, passes now"})
            continue

        before_score = before.get("rubric_score")
        current_score = current.get("rubric_score")
        if before_score is None or current_score is None:
            continue
        delta = round(current_score - before_score, 2)
        if delta <= -1:
            regressions.append(
                {"persona": key, "reason": f"rubric score {delta}", "delta": delta}
            )
        elif delta >= 1:
            improvements.append(
                {"persona": key, "reason": f"rubric score +{delta}", "delta": delta}
            )

    return {
        "regressions": regressions,
        "improvements": improvements,
        "has_regressions": bool(regressions),
    }
