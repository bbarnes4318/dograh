"""Benchmark corpus: manifest schema, preparation, review merge and validation.

    python -m bench.dataset prepare  --input export.csv --recordings-root /data/rec \
        --out /secure/hopstt-corpus --salt-file /secure/hopstt-salt [--sample 260]
    python -m bench.dataset apply-review --corpus /secure/hopstt-corpus \
        --review /secure/hopstt-corpus/review_sheet.csv
    python -m bench.dataset validate --corpus /secure/hopstt-corpus [--check-audio]

See DATASET.md for the format, sampling plan, review procedure and access rules.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import secrets
import sys
import time
from collections import Counter
from pathlib import Path

from .audio import load_audio, write_wav
from .safety import add_safety_args, guard_path, resolve_corpus, write_jsonl_private, write_private

LANGUAGES = ("en", "es")
REFERENCE_STATUSES = ("verified", "unverified")
TAG_VOCABULARY = {
    "older_caller", "fast", "slow", "quiet", "noisy", "distorted", "low_volume", "clipping",
    "interruption", "overlap", "crosstalk", "voicemail", "ivr", "hold_music", "silence",
    "noise_only", "short_yes", "short_no", "names", "spelling", "numbers", "negation",
    "code_switch", "caller", "agent", "long", "speakerphone", "background_tv",
}
TAG_PATTERN = re.compile(r"^accent_[a-z][a-z0-9_]*$")
SILENCE_TAGS = {"silence", "noise_only", "hold_music"}
FIELD_TYPES = (
    "first_name", "last_name", "state", "age", "dob", "phone", "coverage_amount",
    "premium_amount", "medication", "condition", "yes_no", "correction", "callback_request",
    "agent_transfer_consent", "do_not_contact",
)
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_PHONE_LIKE = re.compile(r"\d{7,}")
KNOWN_KEYS = {
    "id", "audio", "language", "channel", "start", "end", "speech_start", "speech_end",
    "reference", "reference_status", "tags", "critical_fields", "baseline", "duration",
    "reviewed_by", "reviewed_at", "notes",
}

# Targets (INVESTIGATION.md §3) used by the distribution report.
TARGET_VERIFIED = 200
TARGET_ES_SHARE = 0.30
ES_SHARE_RANGE = (0.25, 0.40)
TARGET_MIN_ES = 50
TARGET_MIN_SILENCE = 10
TARGET_MIN_NEGATIVE = 20
TARGET_MIN_PER_FIELD = 10
CORE_FIELDS = ("first_name", "last_name", "state", "age", "dob", "phone", "yes_no")


# ---------------------------------------------------------------------------
# Manifest I/O and validation
# ---------------------------------------------------------------------------


def manifest_path(corpus: Path, manifest: str | None = None) -> Path:
    return Path(manifest) if manifest else corpus / "manifest.jsonl"


def load_manifest(path: Path) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{lineno}: invalid JSON ({exc.msg})") from None
            row["_line"] = lineno
            items.append(row)
    return items


def tag_ok(tag: str) -> bool:
    return tag in TAG_VOCABULARY or bool(TAG_PATTERN.match(tag))


def validate_item(item: dict, corpus: Path | None = None, *, check_audio: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    iid = item.get("id")
    if not isinstance(iid, str) or not ID_PATTERN.match(iid):
        errors.append("id must match [A-Za-z0-9_-]{4,64}")
    elif _PHONE_LIKE.search(iid):
        errors.append("id contains a run of 7+ digits (looks like a phone number / call id); use opaque ids")
    audio = item.get("audio")
    if not isinstance(audio, str) or not audio:
        errors.append("audio is required (path relative to the corpus)")
    else:
        if Path(audio).is_absolute() or ".." in Path(audio).parts:
            errors.append("audio must be a relative path inside the corpus")
        if _PHONE_LIKE.search(Path(audio).name):
            errors.append("audio filename contains a run of 7+ digits (phone number / call id?)")
        if check_audio and corpus is not None and not (corpus / audio).is_file():
            errors.append(f"audio file not found: {audio}")
    if item.get("language") not in LANGUAGES:
        errors.append(f"language must be one of {LANGUAGES}")
    ch = item.get("channel")
    if ch is not None and (not isinstance(ch, int) or isinstance(ch, bool) or ch < 0):
        errors.append("channel must be a non-negative integer")
    for a, b in (("start", "end"), ("speech_start", "speech_end")):
        va, vb = item.get(a), item.get(b)
        for k, v in ((a, va), (b, vb)):
            if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0):
                errors.append(f"{k} must be a non-negative number (seconds)")
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and vb <= va:
            errors.append(f"{b} must be greater than {a}")
    ref = item.get("reference")
    if ref is None or not isinstance(ref, str):
        errors.append("reference must be a string (empty for silence/noise-only items)")
        ref = ""
    status = item.get("reference_status")
    if status not in REFERENCE_STATUSES:
        errors.append(f"reference_status must be one of {REFERENCE_STATUSES}")
    tags = item.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        errors.append("tags must be a list of strings")
        tags = []
    bad = [t for t in tags if not tag_ok(t)]
    if bad:
        errors.append(f"unknown tags {bad} (see TAG_VOCABULARY / accent_*)")
    if status == "verified" and not ref.strip() and not (set(tags) & SILENCE_TAGS):
        errors.append("verified item with empty reference must be tagged silence/noise_only/hold_music")
    if ref.strip() and set(tags) & SILENCE_TAGS:
        warnings.append("silence/noise tag but non-empty reference: scored for WER, not hallucination")
    fields = item.get("critical_fields", [])
    if not isinstance(fields, list):
        errors.append("critical_fields must be a list")
        fields = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            errors.append(f"critical_fields[{i}] must be an object")
            continue
        if f.get("type") not in FIELD_TYPES:
            errors.append(f"critical_fields[{i}].type {f.get('type')!r} not in {FIELD_TYPES}")
        if not isinstance(f.get("value"), str) or not f["value"].strip():
            errors.append(f"critical_fields[{i}].value must be a non-empty string")
        if f.get("polarity") not in (None, "negative", "affirmative"):
            errors.append(f"critical_fields[{i}].polarity must be negative|affirmative")
        span = f.get("span")
        if span is not None:
            if not (isinstance(span, dict) and isinstance(span.get("start"), int)
                    and isinstance(span.get("end"), int) and 0 <= span["start"] < span["end"] <= len(ref)):
                errors.append(f"critical_fields[{i}].span must be {{start,end}} char offsets inside reference")
    baseline = item.get("baseline")
    if baseline is not None and not (isinstance(baseline, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in baseline.items())):
        errors.append("baseline must map engine name -> hypothesis text")
    extra = [k for k in item if k not in KNOWN_KEYS and not k.startswith(("x_", "_"))]
    if extra:
        warnings.append(f"unknown keys {extra} (prefix custom keys with x_)")
    return errors, warnings


def is_verified(item: dict) -> bool:
    return item.get("reference_status") == "verified"


def distribution_report(items: list[dict]) -> dict:
    from .metrics import NEG, polarity_of_tokens
    from .normalize import normalize_tokens

    verified = [it for it in items if is_verified(it)]
    lang = Counter(it.get("language") for it in verified)
    tags = Counter(t for it in verified for t in it.get("tags", []))
    ftypes = Counter(f.get("type") for it in verified for f in it.get("critical_fields", []))
    silence = sum(1 for it in verified if not normalize_tokens(it.get("reference", ""), it.get("language", "en")))
    negatives = 0
    for it in verified:
        for f in it.get("critical_fields", []):
            if f.get("type") in ("yes_no", "do_not_contact", "agent_transfer_consent", "correction"):
                pol = f.get("polarity") or polarity_of_tokens(
                    normalize_tokens(f.get("value", ""), it.get("language", "en")), it.get("language", "en"))
                negatives += pol == NEG
    with_baseline = Counter(k for it in verified for k in (it.get("baseline") or {}))
    n = len(verified)
    es_share = lang.get("es", 0) / n if n else 0.0
    shortfalls = []
    if n < TARGET_VERIFIED:
        shortfalls.append(f"verified items {n} < {TARGET_VERIFIED}")
    if lang.get("es", 0) < TARGET_MIN_ES:
        shortfalls.append(f"verified Spanish items {lang.get('es', 0)} < {TARGET_MIN_ES}")
    if n and not ES_SHARE_RANGE[0] <= es_share <= ES_SHARE_RANGE[1]:
        shortfalls.append(f"Spanish share {es_share:.0%} outside {ES_SHARE_RANGE[0]:.0%}-{ES_SHARE_RANGE[1]:.0%} "
                          f"(target ~{TARGET_ES_SHARE:.0%})")
    if silence < TARGET_MIN_SILENCE:
        shortfalls.append(f"silence/noise-only items {silence} < {TARGET_MIN_SILENCE}")
    if negatives < TARGET_MIN_NEGATIVE:
        shortfalls.append(f"negative-polarity fields {negatives} < {TARGET_MIN_NEGATIVE}")
    for ft in CORE_FIELDS:
        if ftypes.get(ft, 0) < TARGET_MIN_PER_FIELD:
            shortfalls.append(f"critical field {ft}: {ftypes.get(ft, 0)} < {TARGET_MIN_PER_FIELD}")
    if n and with_baseline.get("cartesia", 0) < n:
        shortfalls.append(f"cartesia baseline present on {with_baseline.get('cartesia', 0)}/{n} verified items")
    return {
        "items_total": len(items),
        "items_verified": n,
        "items_unverified": len(items) - n,
        "verified_by_language": dict(sorted(lang.items())),
        "spanish_share": round(es_share, 3),
        "verified_by_tag": dict(sorted(tags.items())),
        "verified_by_field_type": dict(sorted(ftypes.items())),
        "silence_items": silence,
        "negative_polarity_fields": negatives,
        "baseline_engines": dict(with_baseline),
        "shortfalls": shortfalls,
    }


def validate_manifest(items: list[dict], corpus: Path | None = None, *, check_audio: bool = False) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for it in items:
        where = f"line {it.get('_line', '?')} ({it.get('id', '?')})"
        e, w = validate_item(it, corpus, check_audio=check_audio)
        errors += [f"{where}: {x}" for x in e]
        warnings += [f"{where}: {x}" for x in w]
        if it.get("id") in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(it.get("id"))
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "distribution": distribution_report(items)}


def clean_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def opaque_id(salt: str, *parts) -> str:
    h = hashlib.sha256(("|".join([salt, *map(str, parts)])).encode()).hexdigest()
    # Letters only in the visible prefix; hex digest can contain digit runs, so
    # map digits to letters to keep ids from ever looking like phone numbers.
    body = h[:16].translate(str.maketrans("0123456789", "ghijklmnop"))
    return f"hs_{body}"


def load_salt(salt_file: str | None, out: Path) -> str:
    env = os.environ.get("HOPSTT_ID_SALT")
    if env:
        return env
    path = Path(salt_file) if salt_file else out / "SALT.private"
    if path.exists():
        return path.read_text().strip()
    salt = secrets.token_hex(16)
    write_private(path, salt + "\n")
    print(f"generated a new id salt at {path} (keep it private; reuse it to get stable ids)", file=sys.stderr)
    return salt


def read_export(path: Path) -> list[dict]:
    if path.suffix.lower() in (".json", ".jsonl"):
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".jsonl":
            return [json.loads(x) for x in text.splitlines() if x.strip()]
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("rows", [])
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(v) -> float | None:
    if v in (None, ""):
        return None
    return float(v)


def _channels_of(path: Path) -> int:
    import wave

    try:
        with wave.open(str(path), "rb") as w:
            return w.getnchannels()
    except Exception:
        import shutil
        import subprocess

        if not shutil.which("ffprobe"):
            return 1
        res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=channels", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        try:
            return int(res.stdout.strip() or 1)
        except ValueError:
            return 1


def _baseline_segments(row: dict) -> list[dict]:
    segs = row.get("cartesia_segments")
    if isinstance(segs, str) and segs.strip():
        segs = json.loads(segs)
    return segs if isinstance(segs, list) else []


def _text_in_window(segs: list[dict], start: float | None, end: float | None, channel: int | None) -> str:
    out = []
    for s in segs:
        if channel is not None and s.get("channel") not in (None, channel):
            continue
        mid = (float(s.get("start", 0)) + float(s.get("end", 0))) / 2
        if (start is None or mid >= start) and (end is None or mid <= end):
            out.append(str(s.get("text", "")).strip())
    return " ".join(x for x in out if x)


def plan_segments(rows: list[dict], *, split_by_baseline: bool, pad: float, recordings_root: Path | None) -> list[dict]:
    """Expand export rows into candidate segments (no audio is cut yet)."""
    plan = []
    for row in rows:
        rec = Path(str(row["recording"]))
        if not rec.is_absolute() and recordings_root is not None:
            rec = recordings_root / rec
        lang = (row.get("language") or "en").strip().lower()[:2]
        tags = row.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[;,]", tags) if t.strip()]
        chan_val = row.get("channel")
        channels = [int(chan_val)] if chan_val not in (None, "") else None
        segs = _baseline_segments(row)
        if channels is None:
            n_ch = _channels_of(rec) if rec.exists() else 1
            channels = list(range(n_ch)) if n_ch > 1 else [None]
        start, end = _f(row.get("start")), _f(row.get("end"))
        for ch in channels:
            if split_by_baseline and segs and start is None and end is None:
                for s in segs:
                    if ch is not None and s.get("channel") not in (None, ch):
                        continue
                    s0 = max(0.0, float(s["start"]) - pad)
                    s1 = float(s["end"]) + pad
                    plan.append({"row": row, "recording": rec, "channel": ch, "start": s0, "end": s1,
                                 "language": lang, "tags": tags, "draft": str(s.get("text", "")).strip()})
            else:
                draft = row.get("cartesia_transcript") or ""
                if segs and not draft:
                    draft = _text_in_window(segs, start, end, ch)
                # A whole-call transcript only matches a leg if the export says which leg.
                if ch is not None and chan_val in (None, "") and not segs:
                    draft = ""
                plan.append({"row": row, "recording": rec, "channel": ch, "start": start, "end": end,
                             "language": lang, "tags": tags, "draft": str(draft).strip()})
    return plan


def sample_segments(plan: list[dict], n: int, en_share: float, seed: int) -> list[dict]:
    """Stratified sample: ~en_share EN, rest ES; round-robin across primary tags for coverage."""
    rng = random.Random(seed)
    out = []
    for lang, quota in (("en", round(n * en_share)), ("es", n - round(n * en_share))):
        pool = [p for p in plan if p["language"] == lang]
        groups: dict[str, list[dict]] = {}
        for p in pool:
            groups.setdefault(p["tags"][0] if p["tags"] else "_untagged", []).append(p)
        for g in groups.values():
            rng.shuffle(g)
        keys = sorted(groups)
        picked = []
        while len(picked) < quota and any(groups.values()):
            for k in keys:
                if groups[k] and len(picked) < quota:
                    picked.append(groups[k].pop())
        if len(picked) < quota:
            print(f"warning: only {len(picked)} {lang} segments available for a quota of {quota}", file=sys.stderr)
        out += picked
    return out


REVIEW_COLUMNS = ["id", "language", "audio", "duration_s", "tags", "draft_reference", "reference",
                  "reference_status", "critical_fields_json", "reviewer", "reviewed_at", "notes"]


def cmd_prepare(args) -> int:
    out = guard_path(args.out, what="output corpus", allow_in_repo=args.allow_in_repo)
    src = guard_path(args.input, what="input export", allow_in_repo=args.allow_in_repo)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    (out / "audio").mkdir(exist_ok=True)
    salt = load_salt(args.salt_file, out)
    rows = read_export(src)
    root = Path(args.recordings_root) if args.recordings_root else None
    plan = plan_segments(rows, split_by_baseline=args.split_by_baseline_segments, pad=args.pad,
                         recordings_root=root)
    if args.sample:
        plan = sample_segments(plan, args.sample, args.en_share, args.seed)
    manifest, id_map, review = [], [], []
    for p in plan:
        call_id = str(p["row"].get("call_id") or p["recording"].name)
        iid = opaque_id(salt, call_id, p["recording"].name, p["channel"], p["start"], p["end"])
        rel = f"audio/{iid}.wav"
        try:
            samples = load_audio(p["recording"], sample_rate=args.sample_rate, channel=p["channel"],
                                 start=p["start"], end=p["end"])
        except Exception as exc:  # keep going; report at the end
            print(f"skip {iid}: cannot cut audio ({type(exc).__name__})", file=sys.stderr)
            continue
        write_wav(out / rel, samples, args.sample_rate)
        os.chmod(out / rel, 0o600)
        duration = round(len(samples) / args.sample_rate, 3)
        item = {"id": iid, "audio": rel, "language": p["language"], "reference": p["draft"],
                "reference_status": "unverified", "tags": p["tags"], "critical_fields": [],
                "duration": duration}
        if p["draft"]:
            item["baseline"] = {"cartesia": p["draft"]}
        manifest.append(item)
        id_map.append({"id": iid, "call_id": call_id, "recording": str(p["recording"]),
                       "channel": p["channel"], "start": p["start"], "end": p["end"]})
        review.append({"id": iid, "language": p["language"], "audio": rel, "duration_s": duration,
                       "tags": ";".join(p["tags"]), "draft_reference": p["draft"], "reference": p["draft"],
                       "reference_status": "unverified", "critical_fields_json": "[]",
                       "reviewer": "", "reviewed_at": "", "notes": ""})
    write_jsonl_private(out / "manifest.jsonl", manifest)
    if not args.no_id_map:
        write_jsonl_private(out / "id_map.private.jsonl", id_map)
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REVIEW_COLUMNS)
    w.writeheader()
    w.writerows(review)
    write_private(out / "review_sheet.csv", buf.getvalue())
    print(f"prepared {len(manifest)} segments in {out} (all unverified; review review_sheet.csv)")
    return 0


def cmd_apply_review(args) -> int:
    corpus = resolve_corpus(args.corpus, allow_in_repo=args.allow_in_repo)
    mpath = manifest_path(corpus, args.manifest)
    items = {it["id"]: clean_item(it) for it in load_manifest(mpath)}
    review = guard_path(args.review, what="review sheet", allow_in_repo=args.allow_in_repo)
    changed = dropped = 0
    with open(review, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            iid = row.get("id", "")
            if iid not in items:
                print(f"warning: review row for unknown id {iid}", file=sys.stderr)
                continue
            status = (row.get("reference_status") or "").strip().lower()
            if status in ("rejected", "exclude", "excluded"):
                items.pop(iid)
                dropped += 1
                continue
            item = items[iid]
            item["tags"] = [t.strip() for t in re.split(r"[;,]", row.get("tags") or "") if t.strip()]
            cf = (row.get("critical_fields_json") or "").strip()
            if cf:
                item["critical_fields"] = json.loads(cf)
            if status == "verified":
                if not (row.get("reviewer") or "").strip():
                    print(f"warning: {iid} marked verified without a reviewer; left unverified", file=sys.stderr)
                    continue
                item["reference"] = row.get("reference") or ""
                item["reference_status"] = "verified"
                item["reviewed_by"] = row["reviewer"].strip()
                item["reviewed_at"] = (row.get("reviewed_at") or "").strip() or time.strftime("%Y-%m-%d")
                changed += 1
    out = Path(args.out) if args.out else mpath
    guard_path(out, what="manifest", allow_in_repo=args.allow_in_repo)
    write_jsonl_private(out, list(items.values()))
    print(f"applied review: {changed} verified, {dropped} dropped -> {out}")
    return 0


def cmd_validate(args) -> int:
    corpus = resolve_corpus(args.corpus, allow_in_repo=args.allow_in_repo)
    items = load_manifest(manifest_path(corpus, args.manifest))
    res = validate_manifest(items, corpus, check_audio=args.check_audio)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for e in res["errors"]:
            print(f"ERROR   {e}")
        for w in res["warnings"]:
            print(f"warning {w}")
        d = res["distribution"]
        print(f"\n{d['items_verified']} verified / {d['items_total']} items; by language {d['verified_by_language']} "
              f"(ES share {d['spanish_share']:.0%})")
        print(f"tags: {d['verified_by_tag']}")
        print(f"critical fields: {d['verified_by_field_type']}")
        print(f"silence items: {d['silence_items']}; negative-polarity fields: {d['negative_polarity_fields']}")
        for s in d["shortfalls"]:
            print(f"SHORTFALL {s}")
    if not res["ok"]:
        return 1
    return 2 if res["distribution"]["shortfalls"] and args.strict else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.dataset", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="draft manifest + per-channel 8 kHz segments + review sheet")
    p.add_argument("--input", required=True, help="CSV/JSON/JSONL export: recording, call_id, language, "
                   "cartesia_transcript, [channel, start, end, tags, cartesia_segments]")
    p.add_argument("--recordings-root", help="base directory for relative recording paths")
    p.add_argument("--out", required=True, help="corpus directory to create (outside any repo)")
    p.add_argument("--salt-file", help="file holding the id salt (default <out>/SALT.private; or $HOPSTT_ID_SALT)")
    p.add_argument("--sample-rate", type=int, default=8000)
    p.add_argument("--split-by-baseline-segments", action="store_true",
                   help="one item per Cartesia segment when the row has cartesia_segments and no start/end")
    p.add_argument("--pad", type=float, default=0.3, help="seconds of padding around baseline segments")
    p.add_argument("--sample", type=int, help="stratified sample size (e.g. 260 to end with >=200 verified)")
    p.add_argument("--en-share", type=float, default=0.7)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--no-id-map", action="store_true", help="do not write id_map.private.jsonl")
    add_safety_args(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("apply-review", help="merge a completed review sheet into the manifest")
    p.add_argument("--corpus")
    p.add_argument("--manifest")
    p.add_argument("--review", required=True)
    p.add_argument("--out", help="output manifest (default: overwrite)")
    add_safety_args(p)
    p.set_defaults(func=cmd_apply_review)

    p = sub.add_parser("validate", help="schema check + distribution report vs targets")
    p.add_argument("--corpus")
    p.add_argument("--manifest")
    p.add_argument("--check-audio", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 2 when targets are not met")
    add_safety_args(p)
    p.set_defaults(func=cmd_validate)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
