"""Manifest validation, distribution report, repo-path guard, prepare/apply-review, audio codecs."""

import csv
import json
import os
import pathlib
import stat
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bench import audio as A  # noqa: E402
from bench import dataset as D  # noqa: E402
from bench.safety import UnsafePathError, find_git_root, guard_path, prepare_output_dir  # noqa: E402

REPO_FILE = pathlib.Path(__file__).resolve()


def good_item(**kw):
    base = {"id": "hs_abcdefgh", "audio": "audio/hs_abcdefgh.wav", "language": "en",
            "reference": "No I am not interested", "reference_status": "verified",
            "tags": ["short_no", "accent_southern_us"],
            "critical_fields": [{"type": "yes_no", "value": "no", "span": {"start": 0, "end": 2}}],
            "baseline": {"cartesia": "no i'm not interested"}}
    base.update(kw)
    return base


def test_valid_item_passes():
    errors, warnings = D.validate_item(good_item())
    assert errors == [] and warnings == []


@pytest.mark.parametrize("patch,needle", [
    ({"id": "call_5551234567"}, "7+ digits"),
    ({"id": "x"}, "id must match"),
    ({"audio": "/abs/path.wav"}, "relative"),
    ({"audio": "../escape.wav"}, "relative"),
    ({"audio": "audio/3055551234.wav"}, "7+ digits"),
    ({"language": "fr"}, "language"),
    ({"channel": -1}, "channel"),
    ({"start": 3.0, "end": 1.0}, "greater"),
    ({"reference_status": "maybe"}, "reference_status"),
    ({"tags": ["loud_music"]}, "unknown tags"),
    ({"reference": "", "tags": []}, "empty reference"),
    ({"critical_fields": [{"type": "ssn", "value": "x"}]}, "type"),
    ({"critical_fields": [{"type": "age", "value": ""}]}, "value"),
    ({"critical_fields": [{"type": "age", "value": "65", "span": {"start": 0, "end": 999}}]}, "span"),
    ({"baseline": {"cartesia": 3}}, "baseline"),
])
def test_invalid_items(patch, needle):
    errors, _ = D.validate_item(good_item(**patch))
    assert any(needle in e for e in errors), errors


def test_silence_item_and_unknown_key_warning():
    errors, warnings = D.validate_item(good_item(reference="", tags=["silence"], critical_fields=[]))
    assert errors == []
    _, warnings = D.validate_item(good_item(foo=1))
    assert any("unknown keys" in w for w in warnings)


def test_distribution_report_flags_shortfalls():
    items = [good_item(id=f"hs_en{i:03d}xx".replace("0", "a"), language="en") for i in range(10)]
    items += [good_item(id=f"hs_es{i:03d}xx".replace("0", "b"), language="es") for i in range(2)]
    items.append(good_item(id="hs_unverif", reference_status="unverified"))
    res = D.validate_manifest(items)
    d = res["distribution"]
    assert d["items_verified"] == 12 and d["items_unverified"] == 1
    assert d["verified_by_language"] == {"en": 10, "es": 2}
    assert any("verified items 12 < 200" in s for s in d["shortfalls"])
    assert any("Spanish share" in s for s in d["shortfalls"])
    assert any("silence" in s for s in d["shortfalls"])
    assert d["negative_polarity_fields"] == 12


def test_duplicate_ids_rejected():
    res = D.validate_manifest([good_item(), good_item()])
    assert not res["ok"] and any("duplicate" in e for e in res["errors"])


# --- repo-path guard ----------------------------------------------------------------


def test_repo_guard_refuses_paths_inside_git_tree(tmp_path):
    root = find_git_root(REPO_FILE)
    assert root is not None  # the test file lives in the dograh checkout
    with pytest.raises(UnsafePathError):
        guard_path(REPO_FILE.parent / "corpus-that-does-not-exist", what="corpus")
    assert guard_path(REPO_FILE.parent / "x", what="corpus", allow_in_repo=True)
    assert guard_path(tmp_path / "out", what="output") == (tmp_path / "out").absolute()


def test_prepare_output_dir_is_private(tmp_path):
    out = prepare_output_dir(str(tmp_path / "runs"), prefix="t")
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_cli_refuses_in_repo_corpus(tmp_path):
    with pytest.raises(UnsafePathError):
        D.main(["validate", "--corpus", str(REPO_FILE.parent)])


# --- prepare / apply-review / validate (synthetic audio only) -------------------------------


def _stereo_wav(path, seconds=2.0, sr=8000):
    left = A.tone(seconds, sr, 440)
    right = A.tone(seconds, sr, 880, amplitude=0.1)
    A.write_wav(path, np.stack([left, right], axis=1).reshape(-1), sr, channels=2)


def test_prepare_apply_review_validate(tmp_path, monkeypatch):
    monkeypatch.delenv("HOPSTT_ID_SALT", raising=False)
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    _stereo_wav(rec_dir / "call-3055551234.wav")
    export = tmp_path / "export.json"
    export.write_text(json.dumps([
        {"recording": "call-3055551234.wav", "call_id": "CA3055551234", "language": "en",
         "tags": "short_no;older_caller",
         "cartesia_segments": [{"start": 0.1, "end": 0.6, "text": "No.", "channel": 0},
                               {"start": 1.0, "end": 1.8, "text": "Okay thanks", "channel": 1}]},
        {"recording": "call-3055551234.wav", "call_id": "CA3055551234", "language": "es",
         "channel": 0, "start": 0.0, "end": 1.0, "cartesia_transcript": "No, gracias"},
    ]))
    corpus = tmp_path / "corpus"
    rc = D.main(["prepare", "--input", str(export), "--recordings-root", str(rec_dir), "--out", str(corpus),
                 "--split-by-baseline-segments"])
    assert rc == 0
    items = D.load_manifest(corpus / "manifest.jsonl")
    assert len(items) == 3  # 2 baseline segments (one per channel) + 1 explicit segment
    for it in items:
        assert "3055551234" not in json.dumps(it)  # no call id / phone digits leak into the manifest
        assert it["reference_status"] == "unverified"
        assert (corpus / it["audio"]).is_file()
        assert A.wav_duration(corpus / it["audio"]) > 0
    assert {it["baseline"]["cartesia"] for it in items} == {"No.", "Okay thanks", "No, gracias"}
    # id map + salt + review sheet are private
    for name in ("id_map.private.jsonl", "SALT.private", "review_sheet.csv", "manifest.jsonl"):
        assert stat.S_IMODE((corpus / name).stat().st_mode) == 0o600, name
    # ids are stable for the same salt
    first_ids = sorted(it["id"] for it in items)
    D.main(["prepare", "--input", str(export), "--recordings-root", str(rec_dir), "--out", str(corpus),
            "--split-by-baseline-segments"])
    assert sorted(it["id"] for it in D.load_manifest(corpus / "manifest.jsonl")) == first_ids

    # Reviewer verifies two rows (correcting one) and rejects one.
    with open(corpus / "review_sheet.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_draft = {r["draft_reference"]: r for r in rows}
    by_draft["No."].update(reference="No, I don't.", reference_status="verified", reviewer="rev1",
                           critical_fields_json=json.dumps([{"type": "yes_no", "value": "no"}]))
    by_draft["No, gracias"].update(reference="No, gracias.", reference_status="verified", reviewer="rev2")
    by_draft["Okay thanks"].update(reference_status="rejected")
    with open(tmp_path / "review_done.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=D.REVIEW_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    assert D.main(["apply-review", "--corpus", str(corpus), "--review", str(tmp_path / "review_done.csv")]) == 0
    items = D.load_manifest(corpus / "manifest.jsonl")
    assert len(items) == 2 and all(it["reference_status"] == "verified" for it in items)
    en = next(it for it in items if it["language"] == "en")
    assert en["reference"] == "No, I don't." and en["baseline"]["cartesia"] == "No."  # baseline never replaced
    assert D.main(["validate", "--corpus", str(corpus), "--check-audio"]) == 0
    assert D.main(["validate", "--corpus", str(corpus), "--strict"]) == 2  # far below 200 items


def test_opaque_id_has_no_digit_runs():
    for i in range(200):
        iid = D.opaque_id("salt", f"call{i}", 0, 1.0, 2.0)
        assert D.ID_PATTERN.match(iid) and not D._PHONE_LIKE.search(iid)


def test_sample_segments_language_split():
    plan = [{"language": "en", "tags": [t]} for t in ("fast", "quiet", "noisy") for _ in range(50)]
    plan += [{"language": "es", "tags": [t]} for t in ("fast", "quiet") for _ in range(50)]
    picked = D.sample_segments(plan, 100, 0.7, seed=1)
    langs = [p["language"] for p in picked]
    assert langs.count("en") == 70 and langs.count("es") == 30
    en_tags = [p["tags"][0] for p in picked if p["language"] == "en"]
    assert min(en_tags.count(t) for t in ("fast", "quiet", "noisy")) >= 23  # round-robin coverage


# --- audio ------------------------------------------------------------------------------------


def test_mulaw_roundtrip_and_known_values():
    x = A.tone(0.1, 8000, 300, amplitude=0.5)
    y = A.ulaw2lin(A.lin2ulaw(x))
    assert np.max(np.abs(x.astype(int) - y.astype(int))) < 0.04 * 32768
    assert A.lin2ulaw(np.array([0], np.int16)) == b"\xff"
    assert A.lin2alaw(np.array([0], np.int16)) == b"\xd5"
    assert len(A.encode(x, "alaw")) == len(x) and len(A.encode(x, "pcm_s16le")) == 2 * len(x)


def test_load_audio_channel_and_segment(tmp_path):
    p = tmp_path / "s.wav"
    _stereo_wav(p, seconds=1.0)
    ch1 = A.load_audio(p, sample_rate=8000, channel=1, start=0.25, end=0.75)
    assert len(ch1) == 4000
    assert A.rms(ch1) < A.rms(A.load_audio(p, sample_rate=8000, channel=0))
    assert os.path.getsize(p) > 0
