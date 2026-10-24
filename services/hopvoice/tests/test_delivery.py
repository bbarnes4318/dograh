import json
from pathlib import Path

from hopvoice import delivery

PACK = Path(__file__).resolve().parents[1] / "bench" / "final_expense_pack.json"


def test_planner_never_returns_or_alters_text():
    # plan() only returns Delivery; it has no field that could carry text.
    fields = set(delivery.Delivery.__dataclass_fields__)
    assert "text" not in fields
    for phrase in json.loads(PACK.read_text())["phrases"]:
        original = phrase["text"]
        plan = delivery.plan(original)
        assert phrase["text"] == original
        for term in plan.emphasis:
            assert term in original.lower()


def test_speech_acts_for_final_expense_lines():
    cases = {
        "Okay.": "acknowledgment",
        "Got it.": "acknowledgment",
        "I'm really sorry to hear about your husband. Take all the time you need.": "empathy",
        "Perfect. I'm going to bring a licensed agent on the line now.": "transfer",
        "I'm showing your age as seventy-two. Is that correct?": "confirmation",
        "Is now still a good time to talk?": "question",
        "I understand. This will only take a second.": "empathy",
        "No problem. The licensed agent can answer that for you.": "reassurance",
        "Oh, that's great news! You should be in good shape then.": "upbeat",
        "Oh, I'm sorry, I said sixty-two — I meant seventy-two.": "correction",
        "Oh, sorry, go ahead.": "yield",
        "Actually, let me correct that. It's the licensed agent who confirms the coverage amount, not me.": "correction",
    }
    for text, act in cases.items():
        assert delivery.plan(text).speech_act == act, text


def test_beat_after_empathy_only():
    empathy = delivery.plan("That's completely understandable.")
    follow = delivery.plan("The licensed agent can go over that with you.", empathy)
    assert follow.pause_before_ms > 0
    assert delivery.plan("Got it.", follow).pause_before_ms == 0


def test_client_delivery_is_clamped_and_filtered():
    parsed = delivery.from_client(
        {"style": "warm", "temperature": 9, "pause_before_ms": 99999, "text": "x"}
    )
    assert parsed.style == "warm"
    assert parsed.temperature == 1.2
    assert parsed.pause_before_ms == 1500
    assert delivery.from_client({"instructions": "be warm"}) is None
