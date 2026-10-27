"""scripts/provision_accent_clones.py is idempotent and never duplicates clones.

A fake Fish client stands in for the SDK, so nothing here needs FISH_API_KEY
or the network.
"""

import csv
import importlib.util
import json
import math
import shutil
import struct
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_DATA_DIR = REPO_ROOT / "api" / "data" / "accent_voices"

_spec = importlib.util.spec_from_file_location(
    "provision_accent_clones", REPO_ROOT / "scripts" / "provision_accent_clones.py"
)
prov = importlib.util.module_from_spec(_spec)
sys.modules["provision_accent_clones"] = prov
_spec.loader.exec_module(prov)

SECRET = "FAKE-FISH-KEY-FOR-REDACTION-TEST"


class FakeAPIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class FakeFish:
    """Mimics the parts of fishaudio.FishAudio the script uses."""

    def __init__(self):
        self.models = []  # (id, title)
        self.created = []
        self.synthesized = []
        self.create_failures = []  # exceptions raised by successive creates
        self.create_succeeds_then_times_out = False
        self.voices = SimpleNamespace(create=self._create, list=self._list)
        self.tts = SimpleNamespace(convert=self._convert)

    def _create(self, *, title, voices, visibility, train_mode, **kwargs):
        assert visibility == "private"
        assert train_mode == "fast"
        assert len(voices) == 1 and isinstance(voices[0], bytes)
        if self.create_failures:
            raise self.create_failures.pop(0)
        voice_id = f"fish{len(self.models) + 1:028d}"
        self.models.append((voice_id, title))
        self.created.append(title)
        if self.create_succeeds_then_times_out:
            self.create_succeeds_then_times_out = False
            raise TimeoutError("read timed out")  # Fish made it; we never heard
        return SimpleNamespace(id=voice_id, title=title)

    def _list(self, *, self_only, title, page_size):
        assert self_only is True
        items = [SimpleNamespace(id=i, title=t) for i, t in self.models if title in t]
        return SimpleNamespace(total=len(items), items=items)

    def _convert(self, *, text, reference_id, format, model):
        assert format == "mp3"
        self.synthesized.append(reference_id)
        return b"ID3" + reference_id.encode()


def _wav(path: Path, seconds=6.0, amplitude=8000, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return path


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "accent_voices"
    (d / "reference_audio").mkdir(parents=True)
    for name in ("accents.json", "area_code_accent_map.json"):
        shutil.copy(REAL_DATA_DIR / name, d / name)
    return d


def _run(data_dir, fish, **kwargs):
    run = prov.ProvisionRun(
        data_dir,
        gateway_factory=lambda: prov.FishGateway(
            fish, secret=SECRET, sleep=lambda s: None
        ),
        delay_s=0,
        **kwargs,
    )
    return run, run.run()


def _registry(data_dir):
    return json.loads((data_dir / "accent_clone_registry.json").read_text())


def _log(data_dir):
    with (data_dir / "clone_provision_log.csv").open() as f:
        return list(csv.DictReader(f))


def _seed_registry(data_dir, accent_id, voice_id):
    _run(data_dir, FakeFish(), only={accent_id})  # writes a full registry
    registry = _registry(data_dir)
    registry[str(accent_id)].update({"fish_voice_id": voice_id, "status": "ready"})
    (data_dir / "accent_clone_registry.json").write_text(json.dumps(registry))


# --- idempotency ------------------------------------------------------------


def test_existing_clone_is_skipped_and_never_recreated(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    _seed_registry(data_dir, 21, "abc123")
    fish = FakeFish()

    _, code = _run(data_dir, fish)

    assert code == 0
    assert fish.created == []
    assert _registry(data_dir)["21"]["fish_voice_id"] == "abc123"
    assert _registry(data_dir)["21"]["status"] == "ready"
    routing = json.loads((data_dir / "accent_routing_map.json").read_text())
    assert {routing[c]["fish_voice_id"] for c in ("423", "729", "865")} == {"abc123"}


def test_only_force_recreates_and_keeps_the_old_id(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    _seed_registry(data_dir, 21, "abc123")
    fish = FakeFish()

    _run(data_dir, fish, force={21})

    entry = _registry(data_dir)["21"]
    assert len(fish.created) == 1
    assert entry["fish_voice_id"] not in ("abc123", None)
    assert entry["previous_fish_voice_ids"] == ["abc123"]
    assert entry["status"] == "ready"


def test_one_clone_per_accent_shared_by_all_its_area_codes(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    fish = FakeFish()

    run, code = _run(data_dir, fish)
    assert code == 0
    assert fish.created == [
        "PVN Local Accent 021 - East Tennessee Appalachian Smoky Mountain"
    ]
    voice_id = fish.models[0][0]

    entry = _registry(data_dir)["21"]
    assert entry == {
        **entry,
        "accent_id": 21,
        "accent_name": "East Tennessee Appalachian / Smoky Mountain",
        "states": ["TN"],
        "area_codes": ["423", "729", "865"],
        "reference_audio": "reference_audio/021_reference.wav",
        "fish_voice_id": voice_id,
        "fish_model_title": (
            "PVN Local Accent 021 - East Tennessee Appalachian Smoky Mountain"
        ),
        "status": "ready",
        "preview": "previews/021.mp3",
    }
    routing = json.loads((data_dir / "accent_routing_map.json").read_text())
    for code in ("423", "729", "865"):
        assert routing[code] == {
            "accent_id": 21,
            "accent_name": "East Tennessee Appalachian / Smoky Mountain",
            "fish_voice_id": voice_id,
        }
    assert routing["212"]["fish_voice_id"] is None
    assert (
        data_dir / "previews" / "021.mp3"
    ).read_bytes() == b"ID3" + voice_id.encode()
    assert fish.synthesized == [voice_id]

    manifest = json.loads((data_dir / "clone_manifest.json").read_text())
    assert manifest["21"]["reference_audio"] == "reference_audio/021_reference.wav"
    assert manifest["6"]["available"] is False

    # A second run changes nothing and creates nothing.
    fish_again = FakeFish()
    _run(data_dir, fish_again)
    assert fish_again.created == [] and fish_again.synthesized == []
    assert _registry(data_dir)["21"]["fish_voice_id"] == voice_id

    rows = _log(data_dir)
    assert [(r["accent_id"], r["status"]) for r in rows] == [("21", "ready")]
    assert list(rows[0]) == list(prov.LOG_FIELDS)


def test_interrupted_run_adopts_the_clone_fish_already_made(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    fish = FakeFish()
    title = prov.fish_model_title(21, "East Tennessee Appalachian / Smoky Mountain")
    fish.models.append(("fishexisting", title))  # created before a crash

    _run(data_dir, fish)

    assert fish.created == []
    assert _registry(data_dir)["21"]["fish_voice_id"] == "fishexisting"


def test_timed_out_create_is_adopted_on_retry_not_duplicated(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    fish = FakeFish()
    fish.create_succeeds_then_times_out = True

    _run(data_dir, fish)

    assert len(fish.created) == 1
    assert _registry(data_dir)["21"]["fish_voice_id"] == fish.models[0][0]


def test_only_limits_the_run(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    _wav(data_dir / "reference_audio" / "006_reference.wav")
    fish = FakeFish()

    _run(data_dir, fish, only={6})

    assert fish.created == ["PVN Local Accent 006 - New York City"]
    assert _registry(data_dir)["21"]["fish_voice_id"] is None


# --- validation and failures -----------------------------------------------


@pytest.mark.parametrize(
    "make, error",
    [
        (lambda p: p.write_bytes(b""), "zero bytes"),
        (lambda p: p.write_bytes(b"RIFF\x00\x00garbage" * 10), "unreadable"),
        (lambda p: _wav(p, seconds=1.0), "too short"),
        (lambda p: _wav(p, amplitude=0), "silent"),
    ],
)
def test_invalid_audio_is_logged_and_the_run_continues(data_dir, make, error):
    make(data_dir / "reference_audio" / "021_reference.wav")
    _wav(data_dir / "reference_audio" / "033_reference.wav")
    fish = FakeFish()

    _, code = _run(data_dir, fish)

    assert code == 0
    registry = _registry(data_dir)
    assert registry["21"]["status"] == "invalid_audio"
    assert error in registry["21"]["error"]
    assert registry["21"]["fish_voice_id"] is None
    assert registry["33"]["status"] == "ready"
    assert fish.created == ["PVN Local Accent 033 - New Orleans Yat-influenced English"]
    assert {r["accent_id"]: r["status"] for r in _log(data_dir)} == {
        "21": "invalid_audio",
        "33": "ready",
    }


def test_ambiguous_reference_files_are_rejected(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    (data_dir / "reference_audio" / "021_reference.mp3").write_bytes(b"ID3" * 100)
    fish = FakeFish()

    _run(data_dir, fish)

    assert fish.created == []
    assert _registry(data_dir)["21"]["status"] == "invalid_audio"
    assert "ambiguous" in _registry(data_dir)["21"]["error"]


def test_api_failure_is_retried_then_recorded_and_the_run_continues(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    _wav(data_dir / "reference_audio" / "033_reference.wav")
    fish = FakeFish()
    fish.create_failures = [
        FakeAPIError(429, "slow down"),  # retried
        FakeAPIError(400, "bad audio"),  # not retryable: accent 21 fails
    ]

    _, code = _run(data_dir, fish)

    assert code == 0
    registry = _registry(data_dir)
    assert registry["21"]["status"] == "failed"
    assert "bad audio" in registry["21"]["error"]
    assert registry["33"]["status"] == "ready"


def test_bad_credentials_abort_without_leaking_the_key(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    _wav(data_dir / "reference_audio" / "033_reference.wav")
    fish = FakeFish()
    fish.create_failures = [FakeAPIError(401, f"invalid key {SECRET}")]

    _, code = _run(data_dir, fish)

    assert code == 2
    assert fish.created == []
    for path in data_dir.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="ignore"), path


def test_failure_messages_are_redacted(data_dir):
    _wav(data_dir / "reference_audio" / "021_reference.wav")
    fish = FakeFish()
    fish.create_failures = [FakeAPIError(400, f"echo {SECRET}")]

    _run(data_dir, fish)

    assert SECRET not in (data_dir / "clone_provision_log.csv").read_text()
    assert "***" in _registry(data_dir)["21"]["error"]


def test_dry_run_writes_nothing_and_needs_no_key(data_dir, monkeypatch):
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    _wav(data_dir / "reference_audio" / "021_reference.wav")

    code = prov.main(["--data-dir", str(data_dir), "--dry-run"])

    assert code == 0
    assert not (data_dir / "accent_clone_registry.json").exists()


def test_no_audio_needs_no_key_and_marks_every_accent_unavailable(
    data_dir, monkeypatch
):
    monkeypatch.delenv("FISH_API_KEY", raising=False)

    assert prov.main(["--data-dir", str(data_dir)]) == 0

    registry = _registry(data_dir)
    assert len(registry) == 75
    assert {e["status"] for e in registry.values()} == {"unavailable"}
    assert {e["fish_voice_id"] for e in registry.values()} == {None}


def test_missing_key_is_a_clear_error_when_work_is_needed(data_dir, monkeypatch):
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    _wav(data_dir / "reference_audio" / "021_reference.wav")

    assert prov.main(["--data-dir", str(data_dir)]) == 2


# --- helpers ----------------------------------------------------------------


def test_titles_are_deterministic():
    assert (
        prov.fish_model_title(21, "East Tennessee Appalachian / Smoky Mountain")
        == "PVN Local Accent 021 - East Tennessee Appalachian Smoky Mountain"
    )
    assert (
        prov.fish_model_title(2, "Northern New England (New Hampshire / Vermont)")
        == "PVN Local Accent 002 - Northern New England New Hampshire Vermont"
    )


def test_parse_ids():
    assert prov.parse_ids("21") == {21}
    assert prov.parse_ids("1-3, 21") == {1, 2, 3, 21}
    assert prov.parse_ids("") == set()


def test_import_audio_uses_the_explicit_mapping(data_dir, tmp_path):
    src = _wav(tmp_path / "incoming" / "knoxville take 3.wav")
    mapping = tmp_path / "mapping.csv"
    mapping.write_text(f"accent_id,source_path\n21,{src}\n999,{src}\n")

    problems = prov.import_audio(mapping, data_dir, set(range(1, 76)))

    assert problems == 1  # the unknown accent id
    assert (data_dir / "reference_audio" / "021_reference.wav").read_bytes() == (
        src.read_bytes()
    )
    # Re-importing the same file is a no-op, not a conflict.
    assert prov.import_audio(mapping, data_dir, set(range(1, 76))) == 1
