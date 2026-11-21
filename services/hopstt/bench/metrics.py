"""Accuracy metrics for the HopSTT benchmark.

Everything is computed on normalized text (``bench.normalize``). Units:

- WER/CER: Levenshtein over normalized word tokens / characters, with
  substitution, deletion and insertion counts. Aggregates are micro-averaged
  (sum of errors / sum of reference words). Items whose normalized reference is
  empty (silence/noise-only) are excluded from WER/CER and scored by the
  hallucination metric instead.
- Critical-field accuracy per field type, numeric-field accuracy.
- Negative-answer flips on polarity fields (yes_no, do_not_contact,
  agent_transfer_consent, correction).
- Silence hallucination: words emitted on empty-reference items.
- Item-level (cluster) bootstrap CIs and paired engine comparisons.
- ``acceptance_gate``: PASS / FAIL / INSUFFICIENT_DATA.

A missing hypothesis (request failed) is scored as an empty hypothesis, so
engine failures cost accuracy instead of silently shrinking the sample.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

import numpy as np

from .normalize import normalize_tokens

NUMERIC_FIELD_TYPES = {"age", "dob", "phone", "coverage_amount", "premium_amount"}
POLARITY_FIELD_TYPES = {"yes_no", "do_not_contact", "agent_transfer_consent", "correction"}
NEG, AFF = "negative", "affirmative"

# ---------------------------------------------------------------------------
# Levenshtein
# ---------------------------------------------------------------------------


@dataclass
class EditCounts:
    sub: int = 0
    dele: int = 0
    ins: int = 0
    ref_len: int = 0

    @property
    def errors(self) -> int:
        return self.sub + self.dele + self.ins

    @property
    def hits(self) -> int:
        return self.ref_len - self.sub - self.dele

    @property
    def rate(self) -> float | None:
        return self.errors / self.ref_len if self.ref_len else None

    def __add__(self, other: "EditCounts") -> "EditCounts":
        return EditCounts(self.sub + other.sub, self.dele + other.dele,
                          self.ins + other.ins, self.ref_len + other.ref_len)


def edit_counts(ref: Sequence, hyp: Sequence) -> EditCounts:
    """Minimum-edit S/D/I counts (memory O(len(hyp))). Ties prefer S, then D, then I."""
    n = len(hyp)
    # Each cell: (cost, sub, del, ins)
    prev = [(j, 0, 0, j) for j in range(n + 1)]
    for i in range(1, len(ref) + 1):
        cur = [(i, 0, i, 0)] + [None] * n  # type: ignore[list-item]
        r = ref[i - 1]
        for j in range(1, n + 1):
            d = prev[j - 1]
            if r == hyp[j - 1]:
                best = d
            else:
                best = (d[0] + 1, d[1] + 1, d[2], d[3])
            up = prev[j]
            if up[0] + 1 < best[0]:
                best = (up[0] + 1, up[1], up[2] + 1, up[3])
            left = cur[j - 1]
            if left[0] + 1 < best[0]:
                best = (left[0] + 1, left[1], left[2], left[3] + 1)
            cur[j] = best
        prev = cur
    _, s, d, ins = prev[n]
    return EditCounts(s, d, ins, len(ref))


def align(ref: Sequence, hyp: Sequence) -> list[tuple[str, int | None, int | None]]:
    """Full alignment: list of (op, ref_index, hyp_index), op in '=', 'S', 'D', 'I'."""
    m, n = len(ref), len(hyp)
    cost = np.zeros((m + 1, n + 1), dtype=np.int32)
    cost[:, 0] = np.arange(m + 1)
    cost[0, :] = np.arange(n + 1)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            diag = cost[i - 1, j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1)
            cost[i, j] = min(diag, cost[i - 1, j] + 1, cost[i, j - 1] + 1)
    ops: list[tuple[str, int | None, int | None]] = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and cost[i, j] == cost[i - 1, j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            ops.append(("=" if ref[i - 1] == hyp[j - 1] else "S", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and cost[i, j] == cost[i - 1, j] + 1:
            ops.append(("D", i - 1, None))
            i -= 1
        else:
            ops.append(("I", None, j - 1))
            j -= 1
    ops.reverse()
    return ops


def aligned_hyp_span(ops, ref_start: int, ref_end: int) -> tuple[int, int]:
    """Hyp token range [a, b) aligned to ref tokens [ref_start, ref_end), incl. inner insertions."""
    hyp_idx: list[int] = []
    inside = False
    for _op, ri, hj in ops:
        if ri is not None:
            inside = ref_start <= ri < ref_end
            if inside and hj is not None:
                hyp_idx.append(hj)
        elif inside and hj is not None:  # insertion after an in-span ref token
            hyp_idx.append(hj)
    if not hyp_idx:
        return 0, 0
    return min(hyp_idx), max(hyp_idx) + 1


def word_counts(ref: str, hyp: str, language: str) -> EditCounts:
    return edit_counts(normalize_tokens(ref, language), normalize_tokens(hyp, language))


def char_counts(ref: str, hyp: str, language: str) -> EditCounts:
    r = " ".join(normalize_tokens(ref, language))
    h = " ".join(normalize_tokens(hyp, language))
    return edit_counts(r, h)


# ---------------------------------------------------------------------------
# Polarity (negative answers)
# ---------------------------------------------------------------------------

# All phrases are in normalized form (lower case, accents stripped, EN
# contractions expanded: "don't" -> "do not").
_POLARITY = {
    "en": {
        "override_aff": ["no problem", "not a problem", "why not", "no doubt", "not bad", "no worries"],
        "neg": ["no", "nope", "nah", "not", "never", "none", "nothing", "negative", "neither",
                "stop calling", "remove me", "take me off", "dont", "cannot"],
        "aff": ["yes", "yeah", "yep", "yup", "ya", "sure", "okay", "correct", "right", "absolutely",
                "definitely", "of course", "uhhuh", "mhm", "i do", "i am", "go ahead",
                "sounds good", "please", "affirmative", "certainly", "exactly", "that is right"],
    },
    "es": {
        "override_aff": ["no hay problema", "como no", "no hay de que", "no hay cuidado"],
        "neg": ["no", "nunca", "jamas", "nada", "ninguno", "ninguna", "ningun", "tampoco",
                "deje de llamar", "dejen de llamar", "nop"],
        "aff": ["si", "claro", "correcto", "exacto", "por supuesto", "de acuerdo", "vale", "aja",
                "okay", "esta bien", "bueno", "sale", "asi es", "afirmativo", "seguro", "mhm"],
    },
}


def _contains(tokens: list[str], phrase: list[str]) -> int:
    """Return index of the first occurrence of ``phrase`` in ``tokens`` or -1."""
    k = len(phrase)
    for i in range(len(tokens) - k + 1):
        if tokens[i:i + k] == phrase:
            return i
    return -1


def polarity_of_tokens(tokens: list[str], language: str) -> str | None:
    """'negative' if any negation cue, else 'affirmative' if any assent cue, else None."""
    lex = _POLARITY["es" if language.startswith("es") else "en"]
    toks = list(tokens)
    had_override = False
    for phrase in lex["override_aff"]:
        p = phrase.split()
        while (i := _contains(toks, p)) >= 0:
            toks[i:i + len(p)] = ["<aff>"]
            had_override = True
    if any(_contains(toks, p.split()) >= 0 for p in lex["neg"]):
        return NEG
    if had_override or any(_contains(toks, p.split()) >= 0 for p in lex["aff"]):
        return AFF
    return None


def polarity(text: str, language: str) -> str | None:
    return polarity_of_tokens(normalize_tokens(text, language), language)


# ---------------------------------------------------------------------------
# Per-item scoring
# ---------------------------------------------------------------------------


@dataclass
class FieldScore:
    type: str
    correct: bool
    numeric: bool
    method: str  # "polarity" | "span" | "anywhere"
    ref_polarity: str | None = None
    hyp_polarity: str | None = None


@dataclass
class ItemScore:
    id: str
    language: str
    tags: list[str]
    missing: bool
    words: EditCounts
    chars: EditCounts
    empty_reference: bool
    hyp_words: int
    fields: list[FieldScore] = field(default_factory=list)

    @property
    def neg_ref(self) -> int:
        return sum(1 for f in self.fields if f.ref_polarity == NEG)

    @property
    def neg_flips(self) -> int:
        return sum(1 for f in self.fields if f.ref_polarity == NEG and f.hyp_polarity != NEG)

    @property
    def neg_to_aff(self) -> int:
        return sum(1 for f in self.fields if f.ref_polarity == NEG and f.hyp_polarity == AFF)

    @property
    def aff_ref(self) -> int:
        return sum(1 for f in self.fields if f.ref_polarity == AFF)

    @property
    def aff_to_neg(self) -> int:
        return sum(1 for f in self.fields if f.ref_polarity == AFF and f.hyp_polarity == NEG)


def _find(tokens: list[str], sub: list[str]) -> list[int]:
    k = len(sub)
    return [i for i in range(len(tokens) - k + 1) if tokens[i:i + k] == sub]


def score_field(fld: dict, reference: str, ref_tokens: list[str], hyp_tokens: list[str],
                ops, language: str) -> FieldScore | None:
    ftype = fld.get("type", "")
    value = str(fld.get("value", ""))
    value_tokens = normalize_tokens(value, language)
    span = fld.get("span")
    # Region of the hypothesis that corresponds to the field.
    region = hyp_tokens
    ref_region = ref_tokens
    method = "anywhere"
    if span and isinstance(span, dict) and "start" in span and "end" in span:
        start, end = int(span["start"]), int(span["end"])
        k0 = len(normalize_tokens(reference[:start], language))
        span_tokens = normalize_tokens(reference[start:end], language)
        # Locate the span tokens in the full normalized reference nearest to k0
        # (digit merging can shift the boundary by one token).
        hits = _find(ref_tokens, span_tokens) if span_tokens else []
        if hits:
            k0 = min(hits, key=lambda h: abs(h - k0))
        k1 = min(len(ref_tokens), k0 + max(1, len(span_tokens)))
        a, b = aligned_hyp_span(ops, k0, k1)
        region = hyp_tokens[a:b]
        ref_region = ref_tokens[k0:k1]
        method = "span"

    if ftype in POLARITY_FIELD_TYPES:
        expected = fld.get("polarity") or polarity_of_tokens(value_tokens, language) \
            or polarity_of_tokens(ref_region, language)
        if expected in (NEG, AFF):
            got = polarity_of_tokens(region, language)
            return FieldScore(ftype, got == expected, False, "polarity", expected, got)
    if not value_tokens:
        return None
    correct = bool(_find(region, value_tokens))
    return FieldScore(ftype, correct, ftype in NUMERIC_FIELD_TYPES, method)


def score_item(item: dict, hypothesis: str | None) -> ItemScore:
    lang = item.get("language", "en")
    reference = item.get("reference") or ""
    ref_tokens = normalize_tokens(reference, lang)
    hyp_tokens = normalize_tokens(hypothesis or "", lang)
    words = edit_counts(ref_tokens, hyp_tokens)
    chars = edit_counts(" ".join(ref_tokens), " ".join(hyp_tokens))
    fields: list[FieldScore] = []
    crit = item.get("critical_fields") or []
    if crit:
        ops = align(ref_tokens, hyp_tokens)
        for fld in crit:
            fs = score_field(fld, reference, ref_tokens, hyp_tokens, ops, lang)
            if fs is not None:
                fields.append(fs)
    return ItemScore(
        id=item["id"], language=lang, tags=list(item.get("tags") or []),
        missing=hypothesis is None, words=words, chars=chars,
        empty_reference=not ref_tokens, hyp_words=len(hyp_tokens), fields=fields,
    )


def score_engine(items: Iterable[dict], hypotheses: dict[str, str | None]) -> list[ItemScore]:
    """Score every item; ids absent from ``hypotheses`` (or None) count as empty output."""
    return [score_item(it, hypotheses.get(it["id"])) for it in items]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _wer_block(scores: list[ItemScore]) -> dict:
    s = [x for x in scores if not x.empty_reference]
    w = sum((x.words for x in s), EditCounts())
    c = sum((x.chars for x in s), EditCounts())
    return {
        "n_items": len(s), "ref_words": w.ref_len, "sub": w.sub, "del": w.dele, "ins": w.ins,
        "wer": _ratio(w.errors, w.ref_len), "ref_chars": c.ref_len,
        "char_errors": c.errors, "cer": _ratio(c.errors, c.ref_len),
    }


def aggregate(scores: list[ItemScore]) -> dict:
    out: dict = {"overall": _wer_block(scores), "n_items": len(scores),
                 "n_missing": sum(1 for x in scores if x.missing)}
    out["by_language"] = {
        lang: _wer_block([x for x in scores if x.language == lang])
        for lang in sorted({x.language for x in scores})
    }
    tags = sorted({t for x in scores for t in x.tags})
    out["by_tag"] = {t: _wer_block([x for x in scores if t in x.tags]) for t in tags}

    per_type: dict[str, dict] = {}
    for x in scores:
        for f in x.fields:
            d = per_type.setdefault(f.type, {"n": 0, "correct": 0})
            d["n"] += 1
            d["correct"] += int(f.correct)
    for d in per_type.values():
        d["accuracy"] = _ratio(d["correct"], d["n"])
    all_f = [f for x in scores for f in x.fields]
    num_f = [f for f in all_f if f.numeric]
    out["fields"] = {
        "by_type": dict(sorted(per_type.items())),
        "n": len(all_f), "accuracy": _ratio(sum(f.correct for f in all_f), len(all_f)),
        "numeric_n": len(num_f), "numeric_accuracy": _ratio(sum(f.correct for f in num_f), len(num_f)),
    }

    neg_ref = sum(x.neg_ref for x in scores)
    flips = sum(x.neg_flips for x in scores)
    to_aff = sum(x.neg_to_aff for x in scores)
    aff_ref = sum(x.aff_ref for x in scores)
    aff_neg = sum(x.aff_to_neg for x in scores)
    out["negation"] = {
        "n_negative_ref": neg_ref,
        "negative_flips": flips,  # negative reference, hypothesis not negative (critical)
        "negative_to_affirmative": to_aff,
        "negative_to_unclear": flips - to_aff,
        "negative_flip_rate": _ratio(flips, neg_ref),
        "negative_to_affirmative_rate": _ratio(to_aff, neg_ref),
        "n_affirmative_ref": aff_ref,
        "affirmative_to_negative": aff_neg,
        "affirmative_to_negative_rate": _ratio(aff_neg, aff_ref),
    }

    sil = [x for x in scores if x.empty_reference]
    out["hallucination"] = {
        "n_items": len(sil),
        "items_with_output": sum(1 for x in sil if x.hyp_words > 0),
        "rate_any_output": _ratio(sum(1 for x in sil if x.hyp_words > 0), len(sil)),
        "words_total": sum(x.hyp_words for x in sil),
        "words_per_item": _ratio(sum(x.hyp_words for x in sil), len(sil)),
    }
    return out


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _vectors(scores: list[ItemScore], metric: str) -> tuple[np.ndarray, np.ndarray]:
    """Per-item numerator/denominator for a ratio metric."""
    if metric == "wer":
        rows = [(x.words.errors, x.words.ref_len) for x in scores if not x.empty_reference]
    elif metric == "cer":
        rows = [(x.chars.errors, x.chars.ref_len) for x in scores if not x.empty_reference]
    elif metric == "field_accuracy":
        rows = [(sum(f.correct for f in x.fields), len(x.fields)) for x in scores]
    elif metric == "numeric_accuracy":
        rows = [(sum(f.correct for f in x.fields if f.numeric), sum(f.numeric for f in x.fields))
                for x in scores]
    elif metric == "negative_flip_rate":
        rows = [(x.neg_flips, x.neg_ref) for x in scores]
    elif metric == "affirmative_to_negative_rate":
        rows = [(x.aff_to_neg, x.aff_ref) for x in scores]
    elif metric == "hallucination_words_per_item":
        rows = [(x.hyp_words, 1) for x in scores if x.empty_reference]
    elif metric == "hallucination_rate_any":
        rows = [(int(x.hyp_words > 0), 1) for x in scores if x.empty_reference]
    else:
        raise ValueError(metric)
    arr = np.array(rows, dtype=float).reshape(-1, 2)
    return arr[:, 0], arr[:, 1]


def _ci(samples: np.ndarray, alpha: float) -> tuple[float | None, float | None]:
    samples = samples[~np.isnan(samples)]
    if samples.size == 0:
        return None, None
    lo, hi = np.percentile(samples, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def bootstrap_ratio(num: np.ndarray, den: np.ndarray, *, n_boot: int = 2000, seed: int = 0,
                    alpha: float = 0.05) -> dict:
    """Item-level bootstrap CI for sum(num)/sum(den). Deterministic for a given seed."""
    num, den = np.asarray(num, float), np.asarray(den, float)
    point = _ratio(float(num.sum()), float(den.sum()))
    n = num.size
    if n == 0 or den.sum() == 0:
        return {"value": point, "ci_low": None, "ci_high": None, "n_units": int(n)}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    with np.errstate(invalid="ignore", divide="ignore"):
        stats = num[idx].sum(1) / den[idx].sum(1)
    lo, hi = _ci(stats, alpha)
    return {"value": point, "ci_low": lo, "ci_high": hi, "n_units": int(n)}


def metric_ci(scores: list[ItemScore], metric: str, *, n_boot: int = 2000, seed: int = 0) -> dict:
    num, den = _vectors(scores, metric)
    return bootstrap_ratio(num, den, n_boot=n_boot, seed=seed)


def paired_difference(scores_a: list[ItemScore], scores_b: list[ItemScore], metric: str, *,
                      n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> dict:
    """metric(A) - metric(B) on the same items, with a paired item-level bootstrap CI."""
    by_b = {x.id: x for x in scores_b}
    pairs = [(a, by_b[a.id]) for a in scores_a if a.id in by_b]
    a_list = [p[0] for p in pairs]
    b_list = [p[1] for p in pairs]
    na, da = _vectors(a_list, metric)
    nb, db = _vectors(b_list, metric)
    va, vb = _ratio(na.sum(), da.sum()), _ratio(nb.sum(), db.sum())
    out = {"metric": metric, "a": va, "b": vb, "diff": None, "ci_low": None, "ci_high": None,
           "n_units": int(na.size)}
    if va is None or vb is None or na.size == 0:
        return out
    out["diff"] = va - vb
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, na.size, size=(n_boot, na.size))
    with np.errstate(invalid="ignore", divide="ignore"):
        stats = na[idx].sum(1) / da[idx].sum(1) - nb[idx].sum(1) / db[idx].sum(1)
    out["ci_low"], out["ci_high"] = _ci(stats, alpha)
    return out


COMPARE_METRICS = ["wer", "cer", "field_accuracy", "numeric_accuracy", "negative_flip_rate",
                   "affirmative_to_negative_rate", "hallucination_words_per_item",
                   "hallucination_rate_any"]


def compare_engines(cand: list[ItemScore], base: list[ItemScore], *, n_boot: int = 2000,
                    seed: int = 0) -> dict:
    """Paired comparison candidate - baseline on the items both were scored on."""
    base_ids = {x.id for x in base}
    cand = [x for x in cand if x.id in base_ids]
    cand_ids = {x.id for x in cand}
    base = [x for x in base if x.id in cand_ids]
    out = {
        "n_items": len(cand),
        "n_items_es": sum(1 for x in cand if x.language == "es"),
        "n_silence_items": sum(1 for x in cand if x.empty_reference),
        "metrics": {m: paired_difference(cand, base, m, n_boot=n_boot, seed=seed) for m in COMPARE_METRICS},
        "by_language": {},
    }
    for lang in sorted({x.language for x in cand}):
        ca = [x for x in cand if x.language == lang]
        ba = [x for x in base if x.language == lang]
        out["by_language"][lang] = paired_difference(ca, ba, "wer", n_boot=n_boot, seed=seed)
    return out


# ---------------------------------------------------------------------------
# Latency summaries
# ---------------------------------------------------------------------------


def summarize(values: Iterable[float | None]) -> dict:
    vals = np.array([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    if vals.size == 0:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    p50, p95, p99 = np.percentile(vals, [50, 95, 99])
    return {"n": int(vals.size), "mean": round(float(vals.mean()), 2), "p50": round(float(p50), 2),
            "p95": round(float(p95), 2), "p99": round(float(p99), 2), "max": round(float(vals.max()), 2)}


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT_DATA"


@dataclass
class GateTargets:
    min_items: int = 200
    min_es_items: int = 50
    min_silence_items: int = 10
    # Absolute non-inferiority margins: the CI upper bound of (candidate - baseline)
    # must stay within the margin for a PASS; a point estimate beyond it is a FAIL.
    wer_margin: float = 0.01
    field_margin: float = 0.01
    flip_margin: float = 0.01
    first_partial_ms: float = 250.0
    finalize_to_final_ms: float = 300.0
    latency_stat: str = "p95"

    def as_dict(self) -> dict:
        return asdict(self)


def _lower_is_better(d: dict, margin: float, label: str) -> tuple[str, str]:
    diff, lo, hi = d.get("diff"), d.get("ci_low"), d.get("ci_high")
    a, b = d.get("a"), d.get("b")
    if diff is None:
        return INSUFFICIENT, f"{label}: not measurable on these items"
    desc = f"{label}: candidate {a:.4f} vs baseline {b:.4f}, diff {diff:+.4f}"
    if lo is not None:
        desc += f" (95% CI {lo:+.4f}..{hi:+.4f})"
    if (lo is not None and lo > 0) or diff > margin:
        return FAIL, desc + " -> worse than baseline"
    if diff <= 0 and hi is not None and hi <= margin:
        return PASS, desc
    return INSUFFICIENT, desc + f" -> within noise (margin {margin}); collect more data"


def _higher_is_better(d: dict, margin: float, label: str) -> tuple[str, str]:
    flipped = dict(d)
    for k in ("diff", "ci_low", "ci_high"):
        flipped[k] = None if d.get(k) is None else -d[k]
    flipped["ci_low"], flipped["ci_high"] = flipped["ci_high"], flipped["ci_low"]
    status, desc = _lower_is_better(flipped, margin, label + " (shortfall)")
    return status, desc


def acceptance_gate(comparison: dict, *, latency: dict | None, targets: GateTargets | None = None,
                    require_latency: bool = True) -> dict:
    """Decide whether a candidate engine may replace the baseline.

    ``comparison`` is ``compare_engines(candidate, baseline)``. ``latency`` maps
    ``first_partial_ms`` and ``finalize_to_final_ms`` to ``summarize()`` dicts
    from a streaming run of the candidate (None for batch-only candidates with
    ``require_latency=False``).
    """
    t = targets or GateTargets()
    checks: dict[str, dict] = {}

    def add(name: str, status: str, detail: str) -> None:
        checks[name] = {"status": status, "detail": detail}

    n, n_es, n_sil = comparison["n_items"], comparison["n_items_es"], comparison["n_silence_items"]
    data_ok = n >= t.min_items and n_es >= t.min_es_items
    add("sample_size", PASS if data_ok else INSUFFICIENT,
        f"{n} verified paired items (need {t.min_items}), {n_es} Spanish (need {t.min_es_items})")

    m = comparison["metrics"]
    add("wer", *_lower_is_better(m["wer"], t.wer_margin, "WER"))
    for lang, d in comparison.get("by_language", {}).items():
        if d.get("ci_low") is not None and d["ci_low"] > 0:
            add(f"wer_{lang}", FAIL, f"WER[{lang}] significantly worse: diff {d['diff']:+.4f} "
                f"(CI {d['ci_low']:+.4f}..{d['ci_high']:+.4f})")
    add("critical_fields", *_higher_is_better(m["field_accuracy"], t.field_margin, "critical-field accuracy"))
    add("negative_flips", *_lower_is_better(m["negative_flip_rate"], t.flip_margin, "negative-flip rate"))

    h_any, h_words = m["hallucination_rate_any"], m["hallucination_words_per_item"]
    if n_sil < t.min_silence_items or h_words.get("diff") is None:
        add("hallucination", INSUFFICIENT, f"{n_sil} silence/noise items (need {t.min_silence_items})")
    elif h_words["diff"] <= 0 and h_any["diff"] <= 0:
        add("hallucination", PASS, f"words/item {h_words['a']:.3f} vs {h_words['b']:.3f}; "
            f"any-output {h_any['a']:.3f} vs {h_any['b']:.3f}")
    else:
        add("hallucination", FAIL, f"words/item {h_words['a']:.3f} vs {h_words['b']:.3f}; "
            f"any-output {h_any['a']:.3f} vs {h_any['b']:.3f} -> more hallucination than baseline")

    if not require_latency:
        add("latency", PASS, "not applicable (batch candidate)")
    elif not latency:
        add("latency", INSUFFICIENT, "no streaming latency measured for the candidate")
    else:
        stat = t.latency_stat
        parts, status = [], PASS
        for key, target in (("first_partial_ms", t.first_partial_ms),
                            ("finalize_to_final_ms", t.finalize_to_final_ms)):
            v = (latency.get(key) or {}).get(stat)
            if v is None:
                status = INSUFFICIENT if status == PASS else status
                parts.append(f"{key} {stat}: not measured")
            else:
                ok = v < target
                status = status if ok else FAIL
                parts.append(f"{key} {stat} {v:.0f} ms (target < {target:.0f})")
        add("latency", status, "; ".join(parts))

    statuses = [c["status"] for c in checks.values()]
    if not data_ok:
        verdict = INSUFFICIENT
    elif FAIL in statuses:
        verdict = FAIL
    elif INSUFFICIENT in statuses:
        verdict = INSUFFICIENT
    else:
        verdict = PASS
    reasons = [f"{k}: {c['status']} - {c['detail']}" for k, c in checks.items() if c["status"] != PASS]
    return {"verdict": verdict, "reasons": reasons, "checks": checks, "targets": t.as_dict()}
