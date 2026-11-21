"""WER/CER, critical fields, negative flips, hallucination, bootstrap and the acceptance gate."""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bench import metrics as M  # noqa: E402


def item(iid, ref, lang="en", tags=(), fields=()):
    return {"id": iid, "language": lang, "reference": ref, "reference_status": "verified",
            "tags": list(tags), "critical_fields": list(fields)}


# --- Levenshtein -------------------------------------------------------------


def test_edit_counts_sdi():
    c = M.edit_counts("a b c d".split(), "a x c d e".split())
    assert (c.sub, c.dele, c.ins, c.ref_len) == (1, 0, 1, 4)
    c = M.edit_counts("a b c".split(), "a c".split())
    assert (c.sub, c.dele, c.ins) == (0, 1, 0)
    assert M.edit_counts([], ["x", "y"]).ins == 2
    assert M.edit_counts(["x"], []).dele == 1
    assert M.edit_counts("same words".split(), "same words".split()).errors == 0


def test_edit_counts_matches_alignment_cost():
    rng = np.random.default_rng(1)
    vocab = list("abcde")
    for _ in range(50):
        r = list(rng.choice(vocab, rng.integers(0, 8)))
        h = list(rng.choice(vocab, rng.integers(0, 8)))
        ops = M.align(r, h)
        assert M.edit_counts(r, h).errors == sum(op != "=" for op, _, _ in ops)


def test_word_and_char_rates_use_normalization():
    w = M.word_counts("I'm 65 years old.", "i am sixty five years old", "en")
    assert w.errors == 0 and w.ref_len == 5
    c = M.char_counts("cat", "cut", "en")
    assert (c.sub, c.ref_len) == (1, 3)


def test_aggregate_micro_average_and_breakdowns():
    items = [item("a1", "one two three four", tags=["fast"]),
             item("a2", "hola amigo", lang="es"),
             item("a3", "", tags=["silence"])]
    hyps = {"a1": "one two three", "a2": "hola amiga", "a3": "thank you"}
    agg = M.aggregate(M.score_engine(items, hyps))
    # EN: "one two three four" -> "1234" vs "123" (digit runs merge: one token error)
    assert agg["by_language"]["en"]["wer"] == 1.0
    assert agg["overall"]["n_items"] == 2  # silence item excluded from WER
    assert agg["by_language"]["es"]["wer"] == pytest.approx(0.5)
    assert agg["hallucination"] == {"n_items": 1, "items_with_output": 1, "rate_any_output": 1.0,
                                    "words_total": 2, "words_per_item": 2.0}
    assert "fast" in agg["by_tag"]


def test_missing_hypothesis_counts_as_deletions():
    items = [item("b1", "hello there")]
    agg = M.aggregate(M.score_engine(items, {}))
    assert agg["overall"]["wer"] == 1.0 and agg["n_missing"] == 1


# --- Critical fields ------------------------------------------------------------


def test_field_scoring_anywhere_and_numeric():
    it = item("c1", "My name is John Smith and I was born March 3rd, 1951 in Texas", fields=[
        {"type": "first_name", "value": "John"},
        {"type": "last_name", "value": "Smith"},
        {"type": "dob", "value": "3/3/1951"},
        {"type": "state", "value": "TX"},
    ])
    s = M.score_item(it, "my name is john smyth and i was born march third nineteen fifty one in texas")
    got = {f.type: f.correct for f in s.fields}
    assert got == {"first_name": True, "last_name": False, "dob": True, "state": True}
    agg = M.aggregate([s])
    assert agg["fields"]["accuracy"] == pytest.approx(0.75)
    assert agg["fields"]["numeric_n"] == 1 and agg["fields"]["numeric_accuracy"] == 1.0


def test_field_span_alignment_rejects_value_elsewhere():
    ref = "I am sixty five and my wife is seventy"
    start = ref.index("sixty five")
    it = item("c2", ref, fields=[{"type": "age", "value": "65",
                                  "span": {"start": start, "end": start + len("sixty five")}}])
    # The value 65 appears in the hypothesis, but at the wife's position (misaligned).
    s = M.score_item(it, "I am seventy five and my wife is 65")
    assert s.fields[0].method == "span" and s.fields[0].correct is False
    s = M.score_item(it, "I am 65 and my wife is seventy")
    assert s.fields[0].correct is True


def test_money_field():
    it = item("c3", "I want twenty five thousand dollars of coverage",
              fields=[{"type": "coverage_amount", "value": "$25,000"}])
    assert M.score_item(it, "I want $25,000 of coverage").fields[0].correct
    assert not M.score_item(it, "I want $2,500 of coverage").fields[0].correct


# --- Polarity / negative flips -------------------------------------------------------


@pytest.mark.parametrize("text,lang,expected", [
    ("No.", "en", M.NEG), ("Nope", "en", M.NEG), ("I'm not interested", "en", M.NEG),
    ("I don't smoke", "en", M.NEG), ("never", "en", M.NEG), ("Yes", "en", M.AFF),
    ("yeah sure", "en", M.AFF), ("uh-huh", "en", M.AFF), ("no problem", "en", M.AFF),
    ("of course not", "en", M.NEG), ("hello", "en", None),
    ("No, no estoy interesada", "es", M.NEG), ("nunca", "es", M.NEG), ("Sí, claro", "es", M.AFF),
    ("¡Cómo no!", "es", M.AFF), ("buenas tardes", "es", None),
])
def test_polarity(text, lang, expected):
    assert M.polarity(text, lang) == expected


def test_negative_flip_counting_both_directions():
    items = [
        item("n1", "No, I don't", fields=[{"type": "yes_no", "value": "no"}]),
        item("n2", "No thank you", fields=[{"type": "do_not_contact", "value": "no"}]),
        item("n3", "Nope", fields=[{"type": "agent_transfer_consent", "value": "no"}]),
        item("n4", "Yes I do", fields=[{"type": "yes_no", "value": "yes"}]),
        item("n5", "no tengo", lang="es", fields=[{"type": "yes_no", "value": "no"}]),
    ]
    hyps = {"n1": "Yes I do", "n2": "thank you", "n3": "nope", "n4": "no I don't", "n5": "no tengo"}
    neg = M.aggregate(M.score_engine(items, hyps))["negation"]
    assert neg["n_negative_ref"] == 4
    assert neg["negative_flips"] == 2  # n1 (-> affirmative), n2 (-> unclear)
    assert neg["negative_to_affirmative"] == 1 and neg["negative_to_unclear"] == 1
    assert neg["negative_flip_rate"] == pytest.approx(0.5)
    assert neg["n_affirmative_ref"] == 1 and neg["affirmative_to_negative"] == 1


def test_polarity_override_from_field():
    it = item("n6", "Take me off your list", fields=[{"type": "do_not_contact", "value": "take me off",
                                                      "polarity": "negative"}])
    assert M.score_item(it, "take me off your list").fields[0].correct
    assert not M.score_item(it, "take me to your list").fields[0].correct


# --- Bootstrap -------------------------------------------------------------------------


def _scores(n, err_rate, seed, lang="en"):
    rng = np.random.default_rng(seed)
    items, hyps = [], {}
    for i in range(n):
        ref = "alpha beta gamma delta epsilon"
        words = ref.split()
        hyp = [("zeta" if rng.random() < err_rate else w) for w in words]
        items.append(item(f"i{i:04d}x", ref, lang=lang if i % 4 else "es"))
        hyps[f"i{i:04d}x"] = " ".join(hyp)
    return items, M.score_engine(items, hyps)


def test_bootstrap_deterministic_with_seed():
    _, s = _scores(60, 0.2, 0)
    a = M.metric_ci(s, "wer", n_boot=500, seed=7)
    b = M.metric_ci(s, "wer", n_boot=500, seed=7)
    c = M.metric_ci(s, "wer", n_boot=500, seed=8)
    assert a == b
    assert a["ci_low"] <= a["value"] <= a["ci_high"]
    assert (a["ci_low"], a["ci_high"]) != (c["ci_low"], c["ci_high"])


def test_paired_difference_sign_and_ci():
    items, worse = _scores(200, 0.3, 1)
    _, better = _scores(200, 0.05, 2)
    d = M.paired_difference(better, worse, "wer", n_boot=500, seed=0)
    assert d["diff"] < 0 and d["ci_high"] < 0
    same = M.paired_difference(worse, worse, "wer", n_boot=200, seed=0)
    assert same["diff"] == 0 and same["ci_low"] == 0 and same["ci_high"] == 0


# --- Acceptance gate ----------------------------------------------------------------------


def _engine(n, wer, neg_flip, halluc, seed, n_es_every=3, n_sil=12):
    rng = np.random.default_rng(seed)
    items, hyps = [], {}
    for i in range(n):
        iid = f"g{i:04d}x"
        lang = "es" if i % n_es_every == 0 else "en"
        if i < n_sil:
            items.append(item(iid, "", lang=lang, tags=["silence"]))
            hyps[iid] = "hello there" if rng.random() < halluc else ""
            continue
        neg = "nunca" if lang == "es" else "never"
        ref = f"alpha beta gamma delta {neg}"
        words = ref.split()
        hyp = [("zeta" if rng.random() < wer else w) for w in words[:-1]]
        hyp.append("yes" if rng.random() < neg_flip else neg)
        items.append(item(iid, ref, lang=lang, fields=[{"type": "yes_no", "value": neg}]))
        hyps[iid] = " ".join(hyp)
    return items, M.score_engine(items, hyps)


GOOD_LATENCY = {"first_partial_ms": {"p95": 200.0}, "finalize_to_final_ms": {"p95": 250.0}}


def test_gate_pass():
    _, base = _engine(300, 0.15, 0.10, 0.5, seed=1)
    _, cand = _engine(300, 0.05, 0.0, 0.0, seed=2)
    g = M.acceptance_gate(M.compare_engines(cand, base, n_boot=500), latency=GOOD_LATENCY)
    assert g["verdict"] == M.PASS, g["reasons"]


def test_gate_fail_on_wer_and_latency():
    _, base = _engine(300, 0.05, 0.0, 0.0, seed=1)
    _, cand = _engine(300, 0.25, 0.0, 0.0, seed=2)
    bad_latency = {"first_partial_ms": {"p95": 200.0}, "finalize_to_final_ms": {"p95": 450.0}}
    g = M.acceptance_gate(M.compare_engines(cand, base, n_boot=500), latency=bad_latency)
    assert g["verdict"] == M.FAIL
    assert g["checks"]["wer"]["status"] == M.FAIL
    assert g["checks"]["latency"]["status"] == M.FAIL


def test_gate_fail_on_negative_flips():
    _, base = _engine(300, 0.05, 0.0, 0.0, seed=1)
    _, cand = _engine(300, 0.05, 0.3, 0.0, seed=1)
    g = M.acceptance_gate(M.compare_engines(cand, base, n_boot=500), latency=GOOD_LATENCY)
    assert g["verdict"] == M.FAIL and g["checks"]["negative_flips"]["status"] == M.FAIL


def test_gate_insufficient_data_small_sample_or_few_spanish():
    _, base = _engine(100, 0.15, 0.1, 0.5, seed=1)
    _, cand = _engine(100, 0.05, 0.0, 0.0, seed=2)
    g = M.acceptance_gate(M.compare_engines(cand, base, n_boot=200), latency=GOOD_LATENCY)
    assert g["verdict"] == M.INSUFFICIENT
    _, base = _engine(300, 0.15, 0.1, 0.5, seed=1, n_es_every=10)  # 30 Spanish
    _, cand = _engine(300, 0.05, 0.0, 0.0, seed=2, n_es_every=10)
    g = M.acceptance_gate(M.compare_engines(cand, base, n_boot=200), latency=GOOD_LATENCY)
    assert g["verdict"] == M.INSUFFICIENT and g["checks"]["sample_size"]["status"] == M.INSUFFICIENT


def test_gate_insufficient_when_difference_is_noise_or_latency_missing():
    _, base = _engine(300, 0.10, 0.0, 0.0, seed=1)
    _, cand = _engine(300, 0.10, 0.0, 0.0, seed=2)  # same distribution, different noise
    cmp_ = M.compare_engines(cand, base, n_boot=500)
    g = M.acceptance_gate(cmp_, latency=GOOD_LATENCY, targets=M.GateTargets(wer_margin=0.0))
    assert g["verdict"] in (M.INSUFFICIENT, M.FAIL)
    assert g["checks"]["wer"]["status"] != M.PASS
    g = M.acceptance_gate(cmp_, latency=None)
    assert g["checks"]["latency"]["status"] == M.INSUFFICIENT
    g = M.acceptance_gate(cmp_, latency=None, require_latency=False)
    assert g["checks"]["latency"]["status"] == M.PASS


def test_summarize_percentiles():
    s = M.summarize(list(range(1, 101)) + [None])
    assert s["n"] == 100 and s["p50"] == pytest.approx(50.5) and s["max"] == 100
    assert M.summarize([])["p95"] is None
