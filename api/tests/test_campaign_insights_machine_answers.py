"""Campaign insights must never count a machine-answered call as a human.

A call the voicemail detector / call hygiene guards hung up on carries
``call_disposition == "voicemail_detected"`` (or ``"answering_bot"``) and a guard
tag. The insights page used to classify from the transcript alone, so those calls
landed in "humans reached" and their disposition showed up in the human outcome
breakdown.
"""

import json
from types import SimpleNamespace

import pytest

from api.routes import reports

# Two lines a person would say, with no voicemail phrase in them — the transcript
# alone reads as a live conversation.
HUMAN_SOUNDING = (
    "[2026-10-16T14:00:01+00:00] assistant: Hi, this is Alex, can you hear me okay?\n"
    "[2026-10-16T14:00:03+00:00] user: Hello?\n"
    "[2026-10-16T14:00:05+00:00] assistant: Great, I'm calling about your coverage.\n"
    "[2026-10-16T14:00:08+00:00] user: Yeah go ahead\n"
)

TRANSFER_OFFER_TO_MACHINE = (
    "[2026-10-16T14:00:01+00:00] assistant: Hi, this is Alex.\n"
    "[2026-10-16T14:00:03+00:00] user: Hello?\n"
    "[2026-10-16T14:00:05+00:00] assistant: Let me bring the licensed agent onto the line.\n"
)


def classify(txt=HUMAN_SOUNDING, dispo="user_hangup", tags=None, **kwargs):
    return reports._classify_run(
        txt,
        dispo,
        kwargs.pop("nodes", []),
        kwargs.pop("transfer_state", None),
        call_tags=tags,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _classify_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dispo", ["voicemail_detected", "answering_bot"])
def test_machine_disposition_is_never_a_human(dispo):
    cls = classify(dispo=dispo)

    assert cls["category"] == "voicemail"
    assert cls["machine_answer"] is True
    assert cls["reason"] is None
    assert cls["opener_drop"] is False
    assert cls["warm_drop"] is False


@pytest.mark.parametrize(
    "tag",
    [
        "voicemail_detected",
        "answering_bot",
        "vm_keyword_guard",
        "vm_after_screener",
        "call_screener_no_pickup",
        "transfer_blocked_machine",
    ],
)
def test_machine_tag_is_never_a_human_even_without_the_disposition(tag):
    cls = classify(dispo="user_hangup", tags=["user_speech", tag])

    assert cls["category"] == "voicemail"
    assert cls["machine_answer"] is True


def test_machine_signal_beats_a_human_llm_judgment():
    cls = classify(dispo="voicemail_detected", category_override="human")

    assert cls["category"] == "voicemail"


def test_machine_signal_beats_the_transcript_heuristics():
    # Five echoed turns would be "echo", an empty reply would be "no_speech" —
    # neither may displace the pipeline's own verdict.
    assert classify("", dispo="voicemail_detected")["category"] == "voicemail"
    only_assistant = "[2026-10-16T14:00:01+00:00] assistant: Hi, this is Alex.\n"
    assert (
        classify(only_assistant, dispo="voicemail_detected")["category"] == "voicemail"
    )


def test_machine_answer_with_no_transcript_still_counts_as_answered():
    # "no_answer" would drop it from the Answered funnel stage; a mailbox did
    # pick up.
    cls = classify("", dispo="voicemail_detected")

    assert cls["category"] == "voicemail"
    assert cls["category"] != "no_answer"


def test_offer_line_read_to_a_machine_is_not_an_interested_transfer():
    cls = classify(TRANSFER_OFFER_TO_MACHINE, dispo="voicemail_detected")

    assert cls["fired_transfer"] is False
    assert cls["transfer_answered"] is False


@pytest.mark.parametrize(
    "dispo,tags",
    [
        ("user_hangup", None),
        ("user_hangup", ["user_speech"]),
        ("user_idle_max_duration_exceeded", ["user_speech", "no_speech_dead_air"]),
        ("end_call_tool", ["closing_line_watchdog", "llm_markup_leak"]),
        ("user_qualified", ["telephony_completed"]),
    ],
)
def test_real_conversations_stay_human(dispo, tags):
    cls = classify(dispo=dispo, tags=tags)

    assert cls["category"] == "human"
    assert cls["machine_answer"] is False


def test_real_transfer_still_counts():
    cls = classify(
        TRANSFER_OFFER_TO_MACHINE, dispo="transfer_call", transfer_state="completed"
    )

    assert cls["category"] == "human"
    assert cls["fired_transfer"] is True
    assert cls["transfer_answered"] is True


def test_voicemail_phrases_still_win_without_a_machine_signal():
    txt = (
        "[2026-10-16T14:00:01+00:00] assistant: Hi, this is Alex.\n"
        "[2026-10-16T14:00:03+00:00] user: Please leave a message after the tone.\n"
    )

    assert classify(txt, dispo="user_hangup")["category"] == "voicemail"


def test_is_machine_answer_ignores_junk_tags():
    assert reports._is_machine_answer(None, None) is False
    assert reports._is_machine_answer("user_hangup", ["user_speech", 7, None]) is False
    assert reports._is_machine_answer("voicemail_detected", []) is True
    assert reports._is_machine_answer(None, ["vm_keyword_guard"]) is True


def test_json_list_accepts_driver_text_or_list():
    assert reports._json_list('["a", "b"]') == ["a", "b"]
    assert reports._json_list(["a"]) == ["a"]
    assert reports._json_list(None) == []
    assert reports._json_list("not json") == []
    assert reports._json_list('{"a": 1}') == []


# ---------------------------------------------------------------------------
# get_campaign_insights — the numbers the page shows
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._rows)


class _FakePipeline:
    def __init__(self, store):
        self._store = store
        self._pending = {}

    def set(self, key, value, ex=None):
        self._pending[key] = value

    async def execute(self):
        self._store.update(self._pending)
        self._pending = {}


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def pipeline(self):
        return _FakePipeline(self.store)


def _row(run_id, *, dispo, tags, nodes=None, transfer_state=None, direction="outbound"):
    from datetime import UTC, datetime

    return {
        "id": run_id,
        "state": "completed",
        "is_completed": True,
        "created_at": datetime(2026, 10, 16, 14, 0, tzinfo=UTC),
        "phone": f"+1555000{run_id:04d}",
        "direction": direction,
        "dispo": dispo,
        "transfer_state": transfer_state,
        "nodes_visited": nodes,
        "call_tags": tags,
        "campaign_id": 1,
        "recording_url": None,
        "transcript_url": None,
        "stored_duration": "12",
    }


@pytest.fixture
def insights(monkeypatch):
    """Run the real route against fake DB rows, Redis and transcript storage."""

    transcripts = {}
    redis = _FakeRedis()

    def get_object(bucket, key):
        run_id = int(key.split("/")[1].split(".")[0])
        return SimpleNamespace(read=lambda: transcripts.get(run_id, "").encode())

    async def no_llm(org_id):
        return None

    monkeypatch.setattr(reports, "_get_llm_cfg", no_llm)
    monkeypatch.setattr(
        reports,
        "storage_fs",
        SimpleNamespace(bucket_name="b", client=SimpleNamespace(get_object=get_object)),
    )

    async def get_redis():
        return redis

    monkeypatch.setattr(reports.rate_limiter, "_get_redis", get_redis)

    async def call(rows, texts):
        transcripts.clear()
        transcripts.update(texts)
        monkeypatch.setattr(
            reports.db_client, "async_session", lambda: _FakeSession(rows)
        )
        return await reports.get_campaign_insights(
            date="2026-10-16",
            start_date=None,
            end_date=None,
            timezone="America/New_York",
            workflow_id=None,
            user=SimpleNamespace(selected_organization_id=1),
        )

    call.redis = redis
    return call


async def test_voicemail_detected_calls_are_not_humans_and_not_in_the_human_breakdown(
    insights,
):
    rows = [
        _row(1, dispo="user_hangup", tags=["user_speech"]),
        _row(2, dispo="voicemail_detected", tags=["voicemail_detected"]),
        _row(
            3,
            dispo="voicemail_detected",
            tags=["vm_keyword_guard", "voicemail_detected"],
        ),
        _row(4, dispo="answering_bot", tags=["answering_bot"]),
        _row(5, dispo="user_hangup", tags=json.dumps(["user_speech"])),
    ]
    texts = {r["id"]: HUMAN_SOUNDING for r in rows}

    result = await insights(rows, texts)

    assert result["kpis"]["answered"] == 5
    assert result["kpis"]["humans"] == 2
    assert result["kpis"]["voicemails"] == 3
    assert {o["result"] for o in result["outcomes"]} == {"Hung up during convo"}
    assert sum(o["count"] for o in result["outcomes"]) == result["kpis"]["humans"]
    assert "voicemail_detected" not in json.dumps(result["outcomes"])
    assert "voicemail_detected" not in json.dumps(result["reasons"])
    assert {c["run_id"]: c["category"] for c in result["calls"]} == {
        1: "human",
        2: "voicemail",
        3: "voicemail",
        4: "voicemail",
        5: "human",
    }
    assert result["kpis"]["human_rate"] == 40.0
    funnel = {s["stage"]: s["count"] for s in result["funnel"]}
    assert funnel["Answered"] == 5 and funnel["Humans reached"] == 2


async def test_stale_cached_human_classification_is_corrected(insights):
    """A run cached as a human before its machine hang-up was read is re-derived."""
    row = _row(7, dispo="voicemail_detected", tags=["voicemail_detected"])
    stale = reports._classify_run(HUMAN_SOUNDING, "user_hangup", [], None)
    stale.pop("machine_answer")
    assert stale["category"] == "human"
    insights.redis.store[f"{reports._CLS_CACHE_PREFIX}7"] = json.dumps(stale)

    # No transcript in storage: the fix must come from the cached transcript.
    result = await insights([row], {})

    assert result["kpis"]["humans"] == 0
    assert result["kpis"]["voicemails"] == 1
    assert result["outcomes"] == []
    fixed = json.loads(insights.redis.store[f"{reports._CLS_CACHE_PREFIX}7"])
    assert fixed["category"] == "voicemail" and fixed["machine_answer"] is True


async def test_machine_calls_do_not_inflate_transfers(insights):
    rows = [
        _row(8, dispo="voicemail_detected", tags=["transfer_blocked_machine"]),
        _row(
            9, dispo="transfer_call", tags=["user_speech"], transfer_state="completed"
        ),
    ]
    texts = {8: TRANSFER_OFFER_TO_MACHINE, 9: TRANSFER_OFFER_TO_MACHINE}

    result = await insights(rows, texts)

    assert result["kpis"]["transfers"] == 1
    assert result["kpis"]["transfers_answered"] == 1
    assert result["kpis"]["humans"] == 1


async def test_llm_budget_is_not_spent_on_machine_hangups(insights, monkeypatch):
    judged = []

    async def llm_cfg(org_id):
        return {"key": "k", "base_url": "http://llm.invalid", "model": "m"}

    async def llm_category(txt, client, cfg, sem):
        judged.append(txt)
        return "human"

    monkeypatch.setattr(reports, "_get_llm_cfg", llm_cfg)
    monkeypatch.setattr(reports, "_llm_category", llm_category)
    rows = [
        _row(10, dispo="voicemail_detected", tags=["voicemail_detected"]),
        _row(11, dispo="user_hangup", tags=["user_speech"]),
    ]

    result = await insights(rows, {10: HUMAN_SOUNDING, 11: HUMAN_SOUNDING})

    assert len(judged) == 1
    assert result["kpis"]["humans"] == 1
    assert result["kpis"]["voicemails"] == 1


# ---------------------------------------------------------------------------
# Inbound and outbound are reported apart
# ---------------------------------------------------------------------------


async def test_inbound_and_outbound_stats_are_reported_apart(insights):
    rows = [
        _row(20, dispo="user_hangup", tags=["user_speech"]),
        _row(21, dispo="user_hangup", tags=["user_speech"]),
        _row(22, dispo="voicemail_detected", tags=["voicemail_detected"]),
        _row(23, dispo="user_hangup", tags=["user_speech"], direction="inbound"),
        _row(
            24,
            dispo="transfer_call",
            tags=[],
            direction="inbound",
            transfer_state="completed",
        ),
        _row(
            25,
            dispo="voicemail_detected",
            tags=["vm_keyword_guard"],
            direction="inbound",
        ),
    ]
    texts = {r["id"]: HUMAN_SOUNDING for r in rows}
    texts[24] = TRANSFER_OFFER_TO_MACHINE

    result = await insights(rows, texts)

    out = result["by_direction"]["outbound"]
    inb = result["by_direction"]["inbound"]

    assert out["kpis"]["dials"] == 3
    assert (out["kpis"]["humans"], out["kpis"]["voicemails"]) == (2, 1)
    assert out["kpis"]["transfers"] == 0

    assert inb["kpis"]["dials"] == 3
    assert (inb["kpis"]["humans"], inb["kpis"]["voicemails"]) == (2, 1)
    assert inb["kpis"]["transfers"] == 1
    assert inb["kpis"]["transfers_answered"] == 1

    # An inbound call is received, not dialed.
    assert out["funnel"][0] == {"stage": "Dials", "count": 3}
    assert inb["funnel"][0] == {"stage": "Calls received", "count": 3}

    # Neither side's breakdown sees the other's calls.
    assert {o["result"] for o in out["outcomes"]} == {"Hung up during convo"}
    assert sum(o["count"] for o in inb["outcomes"]) == inb["kpis"]["humans"]
    assert "voicemail_detected" not in json.dumps([out["outcomes"], inb["outcomes"]])

    # The top-level numbers still cover every call, and add up to the two sides.
    assert result["kpis"]["dials"] == 6
    for key in (
        "dials",
        "answered",
        "humans",
        "voicemails",
        "transfers",
        "transfers_answered",
    ):
        assert result["kpis"][key] == out["kpis"][key] + inb["kpis"][key], key
    assert result["funnel"][0] == {"stage": "Dials", "count": 6}

    # Each call row says which side it was on.
    assert {c["run_id"]: c["direction"] for c in result["calls"]} == {
        20: "Outbound",
        21: "Outbound",
        22: "Outbound",
        23: "Inbound",
        24: "Inbound",
        25: "Inbound",
    }


async def test_a_direction_with_no_calls_reports_zeros(insights):
    result = await insights(
        [_row(30, dispo="user_hangup", tags=["user_speech"])], {30: HUMAN_SOUNDING}
    )

    inb = result["by_direction"]["inbound"]

    assert inb["kpis"]["dials"] == 0 and inb["kpis"]["humans"] == 0
    assert inb["kpis"]["human_rate"] == 0.0
    assert inb["status"]["last_call_at"] is None
    assert inb["outcomes"] == [] and inb["insights"] == []
    assert result["by_direction"]["outbound"]["kpis"]["humans"] == 1


@pytest.mark.parametrize("direction", [None, "", "OUTBOUND"])
async def test_a_call_with_no_clear_direction_counts_as_outbound(insights, direction):
    rows = [_row(31, dispo="user_hangup", tags=["user_speech"], direction=direction)]

    result = await insights(rows, {31: HUMAN_SOUNDING})

    assert result["by_direction"]["outbound"]["kpis"]["dials"] == 1
    assert result["by_direction"]["inbound"]["kpis"]["dials"] == 0
