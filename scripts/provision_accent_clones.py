#!/usr/bin/env python3
"""Provision one private Fish instant-clone voice per regional accent.

Each accent in ``accents.json`` (the authoritative 75-accent dataset) may have
one approved reference recording at ``reference_audio/NNN_reference.<ext>``.
This script uploads each recording to Fish Audio ONCE, through the official
SDK (``fishaudio.FishAudio().voices.create``), creating a reusable private
voice model, and records its id. Calls then route
``area code -> accent_id -> fish_voice_id`` with no cloning in the call path.

Writes, under ``--data-dir``:
  clone_manifest.json          accent -> name/states/area codes/reference audio
  accent_clone_registry.json   accent -> fish_voice_id/title/status (checkpoint)
  accent_routing_map.json      area code -> accent_id/name/fish_voice_id
  clone_provision_log.csv      one row per clone attempt (never any secret)
  previews/NNN.mp3             a short TTS sample synthesized from each clone

Safety:
  * Idempotent: an accent whose registry entry already has a fish_voice_id is
    SKIPPED. Only ``--force <ids>`` creates a replacement clone, and the old
    id is kept in ``previous_fish_voice_ids`` (nothing is ever deleted).
  * Interruption-safe: the registry is rewritten atomically after every
    accent. Before creating a clone the script also looks for a model with
    the same deterministic title on the Fish account and adopts it, so a run
    killed between "Fish created it" and "registry saved" never duplicates.
  * One bad file never stops the run: invalid audio and API failures are
    logged and the remaining accents continue. Bad credentials abort.
  * A lock file prevents two concurrent runs.
  * The only credential source is the FISH_API_KEY environment variable. It
    is never printed, logged or written anywhere.

Usage:
  pip install 'fish-audio-sdk==1.3.0'
  export FISH_API_KEY='...'
  python scripts/provision_accent_clones.py --data-dir api/data/accent_voices
  python scripts/provision_accent_clones.py --data-dir api/data/accent_voices --only 21
  python scripts/provision_accent_clones.py --data-dir api/data/accent_voices --force 21
  python scripts/provision_accent_clones.py --data-dir api/data/accent_voices --dry-run

No FISH_API_KEY is needed when there is nothing to create (e.g. --dry-run, or
no accent has an approved recording yet); the manifest, registry and routing
map are still refreshed.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import csv
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

DATASET_FILE = "accents.json"
AREA_CODE_MAP_FILE = "area_code_accent_map.json"
MANIFEST_FILE = "clone_manifest.json"
REGISTRY_FILE = "accent_clone_registry.json"
ROUTING_MAP_FILE = "accent_routing_map.json"
LOG_FILE = "clone_provision_log.csv"
REFERENCE_DIR = "reference_audio"
PREVIEWS_DIR = "previews"
LOCK_FILE = ".provision.lock"

LOG_FIELDS = (
    "accent_id",
    "accent_name",
    "reference_audio",
    "fish_voice_id",
    "status",
    "attempted_at",
    "error",
)

STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"  # no approved reference recording
STATUS_INVALID_AUDIO = "invalid_audio"
STATUS_FAILED = "failed"

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".webm")
MIN_DURATION_S = 5.0
LONG_DURATION_S = 300.0  # still accepted, but Fish only needs ~10-60s
MAX_BYTES = 100 * 1024 * 1024
SILENCE_PEAK = 100  # 16-bit peak below ~-50 dBFS counts as silent

FISH_TITLE_PREFIX = "PVN Local Accent"
DEFAULT_PREVIEW_MODEL = "s2.1-pro"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
FATAL_STATUS = {401, 402, 403}


class FatalProvisionError(Exception):
    """Stop the whole run (bad credentials, no balance): retrying won't help."""


# --- data -------------------------------------------------------------------


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    """Write via temp file + rename so an interrupted write never corrupts it."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; the app user must read it
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def sorted_by_accent(d: dict) -> dict:
    return {k: d[k] for k in sorted(d, key=int)}


def fish_model_title(accent_id: int, accent_name: str) -> str:
    """``PVN Local Accent 021 - East Tennessee Appalachian Smoky Mountain``."""
    clean = re.sub(r"[^\w\s'ʻ.-]", " ", accent_name)
    clean = re.sub(r"\s+", " ", clean).strip()
    return f"{FISH_TITLE_PREFIX} {accent_id:03d} - {clean}"


def parse_ids(spec: Optional[str]) -> set[int]:
    """``"21"``, ``"1,2,21"`` or ``"1-5,21"`` -> set of accent ids."""
    ids: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            ids.update(range(lo, hi + 1))
        else:
            ids.add(int(part))
    return ids


def find_reference_audio(ref_dir: Path, accent_id: int) -> tuple[Optional[Path], str]:
    """Return ``(path, "")``, or ``(None, reason)`` when missing/ambiguous."""
    prefix = f"{accent_id:03d}_reference"
    try:
        matches = sorted(
            p
            for p in ref_dir.iterdir()
            if p.is_file() and p.stem == prefix and p.suffix.lower() in AUDIO_EXTENSIONS
        )
    except FileNotFoundError:
        matches = []
    if not matches:
        return None, "no approved reference audio"
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        return None, f"ambiguous: several reference files ({names})"
    return matches[0], ""


def build_manifest(dataset: dict, data_dir: Path) -> dict:
    ref_dir = data_dir / REFERENCE_DIR
    manifest = {}
    for acc in dataset["accents"]:
        aid = int(acc["accent_id"])
        path, reason = find_reference_audio(ref_dir, aid)
        entry = {
            "accent_id": aid,
            "accent_name": acc["name"],
            "states": list(acc["states"]),
            "area_codes": list(acc["area_codes"]),
            "reference_audio": (
                path.relative_to(data_dir).as_posix() if path else None
            ),
            "available": path is not None,
        }
        if path is None:
            entry["unavailable_reason"] = reason
        manifest[str(aid)] = entry
    return manifest


def build_routing_map(area_code_map: dict, registry: dict, names: dict) -> dict:
    routing = {}
    for code, aid in sorted(area_code_map["area_code_to_accent_id"].items()):
        entry = registry.get(str(aid)) or {}
        ready = entry.get("status") == STATUS_READY and entry.get("fish_voice_id")
        routing[code] = {
            "accent_id": int(aid),
            "accent_name": names.get(int(aid), ""),
            "fish_voice_id": entry["fish_voice_id"] if ready else None,
        }
    return routing


def import_audio(mapping_csv: Path, data_dir: Path, accent_ids: set[int]) -> int:
    """Copy files to ``NNN_reference.<ext>`` from an explicit accent_id mapping."""
    ref_dir = data_dir / REFERENCE_DIR
    ref_dir.mkdir(parents=True, exist_ok=True)
    problems = 0
    with mapping_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_id = (row.get("accent_id") or "").strip()
            src = Path((row.get("source_path") or "").strip()).expanduser()
            if not raw_id.isdigit() or int(raw_id) not in accent_ids:
                print(f"  IMPORT skip: unknown accent_id {raw_id!r}")
                problems += 1
                continue
            aid = int(raw_id)
            ext = src.suffix.lower()
            if ext not in AUDIO_EXTENSIONS or not src.is_file():
                print(f"  IMPORT {aid:03d}: not an audio file: {src}")
                problems += 1
                continue
            existing, _ = find_reference_audio(ref_dir, aid)
            dest = ref_dir / f"{aid:03d}_reference{ext}"
            if existing and _sha256(existing) == _sha256(src):
                print(f"  IMPORT {aid:03d}: already in place ({existing.name})")
                continue
            if existing:
                print(
                    f"  IMPORT {aid:03d}: {existing.name} already exists with "
                    "different audio; remove it first to replace it"
                )
                problems += 1
                continue
            shutil.copy2(src, dest)
            print(f"  IMPORT {aid:03d}: {src.name} -> {dest.name}")
    return problems


# --- audio validation -------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_wav(path: Path) -> tuple[float, Optional[int]]:
    """``(duration_s, peak)`` for PCM WAV; peak only for 16-bit audio."""
    with wave.open(str(path), "rb") as w:
        rate, frames, width = w.getframerate(), w.getnframes(), w.getsampwidth()
        if rate <= 0:
            raise ValueError("WAV header has no sample rate")
        duration = frames / float(rate)
        peak = None
        if width == 2 and frames:
            samples = array.array("h")
            samples.frombytes(w.readframes(min(frames, rate * 60)))
            if sys.byteorder == "big":
                samples.byteswap()
            peak = max((abs(s) for s in samples), default=0)
    return duration, peak


def _probe_ffprobe(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise ValueError(f"ffprobe could not read it: {out.stderr.strip()[:200]}")
    info = json.loads(out.stdout or "{}")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams") or []):
        raise ValueError("no audio stream")
    return float((info.get("format") or {}).get("duration") or 0.0)


def validate_audio(path: Path) -> tuple[bool, dict]:
    """Return ``(True, info)`` or ``(False, {"error": ...})``; never raises."""
    try:
        size = path.stat().st_size
    except OSError as e:
        return False, {"error": f"not readable: {e}"}
    if size == 0:
        return False, {"error": "file is zero bytes"}
    if size > MAX_BYTES:
        return False, {"error": f"file is too large ({size} bytes)"}

    duration: Optional[float] = None
    peak: Optional[int] = None
    errors = []
    if path.suffix.lower() == ".wav":
        try:
            duration, peak = _probe_wav(path)
        except (wave.Error, EOFError, ValueError, OSError) as e:
            errors.append(f"wav: {e}")
    if duration is None:
        if shutil.which("ffprobe"):
            try:
                duration = _probe_ffprobe(path)
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                errors.append(str(e))
        else:
            errors.append("cannot measure this format without ffprobe (install ffmpeg)")
    if duration is None:
        return False, {"error": "unreadable audio: " + "; ".join(errors)}
    if duration < MIN_DURATION_S:
        return False, {
            "error": f"too short ({duration:.1f}s < {MIN_DURATION_S:.0f}s minimum)"
        }
    if peak is not None and peak < SILENCE_PEAK:
        return False, {"error": "audio is silent"}

    info = {"duration_s": round(duration, 2), "bytes": size, "sha256": _sha256(path)}
    if duration > LONG_DURATION_S:
        info["warning"] = f"long recording ({duration:.0f}s); 10-60s is plenty"
    return True, info


# --- Fish -------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status in RETRY_STATUS:
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(
        names
        & {
            "TimeoutException",
            "TransportError",
            "RateLimitError",
            "ServerError",
            "TimeoutError",
            "ConnectionError",
        }
    )


def _is_fatal(exc: BaseException) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    names = {cls.__name__ for cls in type(exc).__mro__}
    return status in FATAL_STATUS or bool(names & {"AuthenticationError"})


class FishGateway:
    """The few Fish SDK calls provisioning needs, with retries and redaction."""

    def __init__(
        self,
        client: Any,
        *,
        secret: str = "",
        max_tries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = client
        self._secret = secret
        self._max_tries = max(1, max_tries)
        self._sleep = sleep

    @classmethod
    def from_env(cls, max_tries: int) -> "FishGateway":
        key = os.environ.get("FISH_API_KEY", "").strip()
        if not key:
            raise FatalProvisionError(
                "FISH_API_KEY is not set; export it in this shell (never commit it)"
            )
        try:
            from fishaudio import FishAudio
        except ImportError:
            raise FatalProvisionError(
                "the Fish SDK is not installed: pip install 'fish-audio-sdk==1.3.0'"
            )
        return cls(FishAudio(api_key=key), secret=key, max_tries=max_tries)

    def redact(self, text: str) -> str:
        text = str(text)
        if self._secret:
            text = text.replace(self._secret, "***")
        return text[:500]

    def _call(self, label: str, fn: Callable[[], Any], before_retry=None) -> Any:
        for attempt in range(1, self._max_tries + 1):
            try:
                return fn()
            except Exception as e:
                if _is_fatal(e):
                    raise FatalProvisionError(
                        f"{label}: Fish rejected the credentials/account: "
                        f"{self.redact(e)}"
                    ) from None
                if not _is_retryable(e) or attempt == self._max_tries:
                    raise RuntimeError(f"{label}: {self.redact(e)}") from None
                wait = min(60.0, 2.0**attempt) + random.uniform(0, 1)
                print(
                    f"     retry {attempt}/{self._max_tries - 1} in {wait:.1f}s "
                    f"({self.redact(e)})",
                    flush=True,
                )
                self._sleep(wait)
                if before_retry is not None:
                    found = before_retry()
                    if found is not None:
                        return found
        raise AssertionError("unreachable")

    def find_existing(self, title: str) -> Optional[str]:
        """Id of a model on this account with exactly ``title``, if any."""

        def lookup():
            page = self._client.voices.list(self_only=True, title=title, page_size=50)
            matches = [v for v in page.items if getattr(v, "title", None) == title]
            if len(matches) > 1:
                print(
                    f"     WARNING: {len(matches)} Fish models titled {title!r}; "
                    f"adopting {matches[0].id}",
                    flush=True,
                )
            return matches[0].id if matches else None

        return self._call("look up existing clone", lookup)

    def create_clone(
        self, *, title: str, audio: bytes, description: str, tags: list[str]
    ) -> str:
        def create():
            voice = self._client.voices.create(
                title=title,
                voices=[audio],
                description=description,
                tags=tags,
                visibility="private",
                train_mode="fast",
                enhance_audio_quality=False,
            )
            return voice.id

        # A create that timed out may still have succeeded on Fish's side;
        # look for it by title before trying again so it is never duplicated.
        def adopt_if_created():
            with contextlib.suppress(Exception):
                return self.find_existing(title)
            return None

        return self._call("create clone", create, before_retry=adopt_if_created)

    def synthesize(self, *, voice_id: str, text: str, model: str) -> bytes:
        def convert():
            return self._client.tts.convert(
                text=text, reference_id=voice_id, format="mp3", model=model
            )

        return self._call("synthesize preview", convert)


# --- provisioning -----------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProvisionRun:
    def __init__(
        self,
        data_dir: Path,
        *,
        only: Optional[set[int]] = None,
        force: Optional[set[int]] = None,
        dry_run: bool = False,
        previews: bool = True,
        preview_model: str = DEFAULT_PREVIEW_MODEL,
        gateway_factory: Optional[Callable[[], FishGateway]] = None,
        delay_s: float = 1.0,
    ):
        self.data_dir = Path(data_dir)
        self.dataset = load_json(self.data_dir / DATASET_FILE)
        if not self.dataset:
            raise FatalProvisionError(f"missing {self.data_dir / DATASET_FILE}")
        self.area_code_map = load_json(self.data_dir / AREA_CODE_MAP_FILE)
        if not self.area_code_map:
            raise FatalProvisionError(f"missing {self.data_dir / AREA_CODE_MAP_FILE}")
        self.accents = {int(a["accent_id"]): a for a in self.dataset["accents"]}
        self.names = {aid: a["name"] for aid, a in self.accents.items()}
        self.only = set(only or ())
        self.force = set(force or ())
        unknown = (self.only | self.force) - set(self.accents)
        if unknown:
            raise FatalProvisionError(f"unknown accent ids: {sorted(unknown)}")
        self.dry_run = dry_run
        self.previews = previews
        self.preview_model = preview_model
        self.preview_text = self.dataset.get("preview_text") or (
            "Hi, this is a quick call about the request you sent in. "
            "Do you have a minute?"
        )
        self._gateway_factory = gateway_factory or (lambda: FishGateway.from_env(5))
        self._gateway: Optional[FishGateway] = None
        self.delay_s = delay_s
        self.registry_path = self.data_dir / REGISTRY_FILE
        self.registry: dict = load_json(self.registry_path, {}) or {}
        self.log_rows: list[dict] = []
        self.created = 0
        self.adopted = 0

    @property
    def gateway(self) -> FishGateway:
        if self._gateway is None:
            self._gateway = self._gateway_factory()
        return self._gateway

    def selected(self) -> list[int]:
        ids = sorted(self.accents)
        if self.only or self.force:
            ids = [aid for aid in ids if aid in self.only or aid in self.force]
        return ids

    def checkpoint(self) -> None:
        if not self.dry_run:
            atomic_write_json(self.registry_path, sorted_by_accent(self.registry))

    def log(self, entry: dict, status: str, error: str = "") -> None:
        self.log_rows.append(
            {
                "accent_id": entry["accent_id"],
                "accent_name": entry["accent_name"],
                "reference_audio": entry.get("reference_audio") or "",
                "fish_voice_id": entry.get("fish_voice_id") or "",
                "status": status,
                "attempted_at": _now(),
                "error": error,
            }
        )

    def _base_entry(self, aid: int, manifest_entry: dict) -> dict:
        acc = self.accents[aid]
        entry = dict(self.registry.get(str(aid)) or {})
        entry.update(
            {
                "accent_id": aid,
                "accent_name": acc["name"],
                "states": list(acc["states"]),
                "area_codes": list(acc["area_codes"]),
                "reference_audio": manifest_entry["reference_audio"],
                "fish_voice_id": entry.get("fish_voice_id") or None,
                "fish_model_title": fish_model_title(aid, acc["name"]),
            }
        )
        return entry

    def run(self) -> int:
        manifest = build_manifest(self.dataset, self.data_dir)
        if not self.dry_run:
            atomic_write_json(self.data_dir / MANIFEST_FILE, manifest)
            (self.data_dir / PREVIEWS_DIR).mkdir(exist_ok=True)

        # Every accent gets a registry entry so the file always shows all 75.
        for aid in sorted(self.accents):
            entry = self._base_entry(aid, manifest[str(aid)])
            if not entry.get("status"):
                entry["status"] = (
                    STATUS_READY if entry["fish_voice_id"] else STATUS_UNAVAILABLE
                )
            self.registry[str(aid)] = entry

        fatal: Optional[str] = None
        try:
            for aid in self.selected():
                self.provision_one(aid, manifest[str(aid)])
        except FatalProvisionError as e:
            fatal = str(e)
            print(f"\nABORTED: {fatal}", flush=True)
        finally:
            self.checkpoint()
            self.write_outputs()

        ready = sorted(
            int(k)
            for k, e in self.registry.items()
            if e.get("status") == STATUS_READY and e.get("fish_voice_id")
        )
        missing = sorted(set(self.accents) - set(ready))
        print(
            f"\nAccent clones provisioned: {len(ready)}/{len(self.accents)} "
            f"(created {self.created}, adopted {self.adopted} this run)"
        )
        if missing:
            print("Failed/missing accent IDs: " + ", ".join(map(str, missing)))
        return 2 if fatal else 0

    def provision_one(self, aid: int, manifest_entry: dict) -> None:
        entry = self.registry[str(aid)]
        tag = f"[{aid:03d}] {entry['accent_name']}"
        forced = aid in self.force

        if entry.get("fish_voice_id") and not forced:
            print(f"{tag}: SKIP (fish_voice_id {entry['fish_voice_id']})", flush=True)
            self.ensure_preview(entry, tag)
            return

        if not manifest_entry["available"]:
            reason = manifest_entry.get("unavailable_reason", "")
            entry["fish_voice_id"] = None
            if reason.startswith("ambiguous"):
                entry.update({"status": STATUS_INVALID_AUDIO, "error": reason})
                self.log(entry, STATUS_INVALID_AUDIO, reason)
            else:
                entry["status"] = STATUS_UNAVAILABLE
                entry.pop("error", None)
            print(f"{tag}: UNAVAILABLE ({reason})", flush=True)
            if forced:
                print(f"{tag}: --force ignored: no reference audio", flush=True)
            return

        audio_path = self.data_dir / manifest_entry["reference_audio"]
        ok, info = validate_audio(audio_path)
        if not ok:
            entry.update({"status": STATUS_INVALID_AUDIO, "error": info["error"]})
            if not entry.get("fish_voice_id"):
                entry["fish_voice_id"] = None
            print(f"{tag}: INVALID AUDIO ({info['error']})", flush=True)
            self.log(entry, STATUS_INVALID_AUDIO, info["error"])
            self.checkpoint()
            return
        if info.get("warning"):
            print(f"{tag}: note: {info['warning']}", flush=True)

        title = entry["fish_model_title"]
        action = "RECREATE (--force)" if forced else "CREATE"
        print(f"{tag}: {action} {title!r} from {audio_path.name}", flush=True)
        if self.dry_run:
            return

        previous = entry.get("fish_voice_id")
        try:
            voice_id = None if forced else self.gateway.find_existing(title)
            if voice_id:
                self.adopted += 1
                print(f"{tag}: ADOPT existing Fish model {voice_id}", flush=True)
            else:
                voice_id = self.gateway.create_clone(
                    title=title,
                    audio=audio_path.read_bytes(),
                    description=(
                        f"{entry['accent_name']} | {', '.join(entry['states'])} | "
                        f"area codes {', '.join(entry['area_codes'])}"
                    ),
                    tags=["pvn-local-accent", f"accent-{aid:03d}"],
                )
                self.created += 1
                print(f"{tag}: OK fish_voice_id={voice_id}", flush=True)
        except FatalProvisionError:
            raise
        except Exception as e:
            err = str(e)
            entry.update({"status": STATUS_FAILED, "error": err})
            if not previous:
                entry["fish_voice_id"] = None
            print(f"{tag}: FAILED ({err})", flush=True)
            self.log(entry, STATUS_FAILED, err)
            self.checkpoint()
            return

        if previous and previous != voice_id:
            history = entry.setdefault("previous_fish_voice_ids", [])
            if previous not in history:
                history.append(previous)
        entry.update(
            {
                "fish_voice_id": voice_id,
                "status": STATUS_READY,
                "provisioned_at": _now(),
                "reference_sha256": info["sha256"],
                "reference_duration_s": info["duration_s"],
            }
        )
        entry.pop("error", None)
        entry.pop("preview", None)  # a recreated clone needs a fresh preview
        self.log(entry, STATUS_READY)
        self.checkpoint()  # the id is safe on disk before anything else runs
        self.ensure_preview(entry, tag, regenerate=True)
        if self.delay_s:
            time.sleep(self.delay_s)

    def ensure_preview(self, entry: dict, tag: str, regenerate: bool = False) -> None:
        if not self.previews or self.dry_run or not entry.get("fish_voice_id"):
            return
        rel = f"{PREVIEWS_DIR}/{entry['accent_id']:03d}.mp3"
        path = self.data_dir / rel
        if path.is_file() and path.stat().st_size > 0 and not regenerate:
            entry["preview"] = rel
            return
        try:
            audio = self.gateway.synthesize(
                voice_id=entry["fish_voice_id"],
                text=self.preview_text,
                model=self.preview_model,
            )
            if not audio:
                raise RuntimeError("synthesize preview: Fish returned no audio")
            path.write_bytes(audio)
            entry["preview"] = rel
            entry.pop("preview_error", None)
            print(f"{tag}: preview -> {rel}", flush=True)
        except FatalProvisionError:
            raise
        except Exception as e:
            entry["preview_error"] = str(e)
            print(f"{tag}: preview FAILED ({e}); the clone itself is fine", flush=True)
        self.checkpoint()

    def write_outputs(self) -> None:
        if self.dry_run:
            return
        atomic_write_json(
            self.data_dir / ROUTING_MAP_FILE,
            build_routing_map(self.area_code_map, self.registry, self.names),
        )
        log_path = self.data_dir / LOG_FILE
        new = not log_path.exists()
        with log_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerows(self.log_rows)


@contextlib.contextmanager
def run_lock(data_dir: Path) -> Iterator[None]:
    import fcntl

    with (data_dir / LOCK_FILE).open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FatalProvisionError(
                "another provisioning run holds the lock; wait for it to finish"
            )
        yield


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Provision one private Fish instant clone per regional accent."
    )
    ap.add_argument("--data-dir", default="api/data/accent_voices", type=Path)
    ap.add_argument("--only", default="", help="accent ids, e.g. 21 or 1-5,21")
    ap.add_argument(
        "--force",
        default="",
        help="accent ids to RE-CLONE even though they have a voice (old id kept)",
    )
    ap.add_argument("--dry-run", action="store_true", help="validate, write nothing")
    ap.add_argument("--no-previews", action="store_true")
    ap.add_argument("--preview-model", default=DEFAULT_PREVIEW_MODEL)
    ap.add_argument("--max-tries", type=int, default=5)
    ap.add_argument(
        "--import-audio",
        type=Path,
        help="CSV (accent_id,source_path) to copy into reference_audio/ first",
    )
    a = ap.parse_args(argv)

    data_dir: Path = a.data_dir
    if not data_dir.is_dir():
        print(f"data dir not found: {data_dir}", file=sys.stderr)
        return 2
    try:
        with run_lock(data_dir):
            if a.import_audio:
                dataset = load_json(data_dir / DATASET_FILE) or {"accents": []}
                ids = {int(x["accent_id"]) for x in dataset["accents"]}
                print(f"Importing reference audio from {a.import_audio}")
                if import_audio(a.import_audio, data_dir, ids):
                    print("Some files were not imported (see above).")
            run = ProvisionRun(
                data_dir,
                only=parse_ids(a.only),
                force=parse_ids(a.force),
                dry_run=a.dry_run,
                previews=not a.no_previews,
                preview_model=a.preview_model,
                gateway_factory=lambda: FishGateway.from_env(a.max_tries),
            )
            return run.run()
    except FatalProvisionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
