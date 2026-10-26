"""Local-accent Fish voices: 75 US regional accents x male/female.

The voices are original Fish Voice Design voices provisioned by
``scripts/provision_accent_voices.py``, which writes
``api/data/accent_voices/accent_voice_registry.json``. They are PRIVATE Fish
models, so the org's Fish TTS key must belong to the same Fish account.

A Fish TTS config can store a concrete reference_id (one accent, every call)
or one of ``AUTO_VOICE_VALUES``, in which case ``resolve_accent_voice`` picks
the accent from the prospect's area code when the call's pipeline is built:

* inbound calls use ``caller_number``, outbound ``called_number`` (or the
  legacy ``phone_number``), via ``resolve_call_recipient``;
* toll-free, Canadian, unknown or missing numbers use the default accent
  (68, Colorado / Central Rockies, the most neutral of the set).

The data files are loaded once on import. A missing registry (voices not
provisioned yet) is not an error: the voice list only holds the Auto entries
and auto calls fall back to Fish's default voice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from loguru import logger

from api.services.sms.recipient import (
    RecipientResolutionError,
    resolve_call_recipient,
    snapshot_call_parties,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "accent_voices"
REGISTRY_FILE = "accent_voice_registry.json"
AREA_CODE_MAP_FILE = "area_code_accent_map.json"
SPEC_FILE = "accent_voices.json"
PREVIEWS_DIR_NAME = "previews"

AUTO_VOICE_VALUES = frozenset({"auto:female", "auto:male"})
GENDERS = ("female", "male")
FALLBACK_DEFAULT_ACCENT_ID = 68
PREVIEW_ROUTE = "api/v1/public/accent-voices/previews"
PREVIEW_FILENAME_RE = re.compile(r"^\d{2}_(male|female)\.(mp3|wav)$")

_AREA_CODE_RE = re.compile(r"^\d{3}$")


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read accent voice data {path}: {e}")
        return None


class AccentVoiceCatalog:
    """In-memory view of the accent voice data files, built for O(1) lookups."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.previews_dir = self.data_dir / PREVIEWS_DIR_NAME

        amap = _read_json(self.data_dir / AREA_CODE_MAP_FILE) or {}
        self.area_code_to_accent: dict[str, int] = {
            str(code): int(aid)
            for code, aid in (amap.get("area_code_to_accent_id") or {}).items()
        }
        self.default_accent_id: int = int(
            amap.get("default_accent_id") or FALLBACK_DEFAULT_ACCENT_ID
        )

        # Accent names come from the spec so an auto pick is labelled even
        # before (or without) that accent's voices being provisioned.
        self.accent_names: dict[int, str] = {}
        spec = _read_json(self.data_dir / SPEC_FILE) or {}
        for acc in spec.get("accents") or []:
            self.accent_names[int(acc["accent_id"])] = acc["name"]

        registry = _read_json(self.data_dir / REGISTRY_FILE)
        if registry is None:
            logger.warning(
                f"Accent voice registry not found at {self.data_dir / REGISTRY_FILE}; "
                "run scripts/provision_accent_voices.py. Auto accent voices will "
                "use Fish's default voice until it exists."
            )
            registry = {}
        self.accents: dict[int, dict] = {}
        for key, entry in registry.items():
            aid = int(entry.get("accent_id") or key)
            self.accents[aid] = entry
            if entry.get("name"):
                self.accent_names[aid] = entry["name"]

        try:
            self.preview_files = {
                p.name for p in self.previews_dir.iterdir() if p.is_file()
            }
        except OSError:
            self.preview_files = set()

    def voice_id(self, accent_id: int, gender: str) -> str:
        entry = self.accents.get(accent_id) or {}
        return ((entry.get(gender) or {}).get("voice_id")) or ""

    def preview_filename(self, accent_id: int, gender: str) -> Optional[str]:
        for ext in ("mp3", "wav"):
            name = f"{accent_id:02d}_{gender}.{ext}"
            if name in self.preview_files:
                return name
        return None


_catalog = AccentVoiceCatalog(DATA_DIR)


def get_catalog() -> AccentVoiceCatalog:
    return _catalog


def _auto_entries(catalog: AccentVoiceCatalog) -> list[dict]:
    default_name = catalog.accent_names.get(catalog.default_accent_id, "default")
    return [
        {
            "voice_id": f"auto:{gender}",
            "name": f"Auto — local accent by area code ({gender.title()})",
            "gender": gender,
            "language": "en-US",
            "description": (
                "Matches each prospect's area code on every call, inbound and "
                f"outbound. Unknown or toll-free numbers use {default_name}."
            ),
        }
        for gender in GENDERS
    ]


def _accent_voice_entries(catalog: AccentVoiceCatalog, base_url: str) -> list[dict]:
    voices = []
    for aid in sorted(catalog.accents):
        entry = catalog.accents[aid]
        name = catalog.accent_names.get(aid) or entry.get("name") or f"Accent {aid}"
        area_codes = entry.get("area_codes") or []
        for gender in GENDERS:
            voice_id = catalog.voice_id(aid, gender)
            if not voice_id:
                continue
            voice = {
                "voice_id": voice_id,
                "name": f"{name} ({gender.title()})",
                "accent": name,
                "gender": gender,
                "language": "en-US",
                "description": (
                    f"{entry.get('states', '')} · area codes {', '.join(area_codes)}"
                ),
            }
            preview = catalog.preview_filename(aid, gender)
            if preview:
                voice["preview_url"] = f"{base_url}{PREVIEW_ROUTE}/{preview}"
            voices.append(voice)
    return voices


def list_fish_accent_voices(
    base_url: str,
    *,
    q: Optional[str] = None,
    gender: Optional[str] = None,
    accent: Optional[str] = None,
    catalog: Optional[AccentVoiceCatalog] = None,
) -> list[dict]:
    """VoiceInfo-shaped dicts: the two Auto entries, then every registered voice.

    ``q`` is a case-insensitive substring match on name/accent/description;
    ``gender`` and ``accent`` are case-insensitive exact matches. The Auto
    entries are always returned first, whatever the filters.
    """
    catalog = catalog or _catalog
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    q_lower = (q or "").strip().lower()
    gender_lower = (gender or "").strip().lower()
    accent_lower = (accent or "").strip().lower()

    def matches(voice: dict) -> bool:
        if q_lower and not any(
            q_lower in (voice.get(k) or "").lower()
            for k in ("name", "accent", "description")
        ):
            return False
        if gender_lower and (voice.get("gender") or "").lower() != gender_lower:
            return False
        if accent_lower and (voice.get("accent") or "").lower() != accent_lower:
            return False
        return True

    return _auto_entries(catalog) + [
        v for v in _accent_voice_entries(catalog, base_url) if matches(v)
    ]


def fish_accent_voice_accents(
    catalog: Optional[AccentVoiceCatalog] = None,
) -> list[str]:
    """Sorted accent names that have at least one registered voice."""
    catalog = catalog or _catalog
    return sorted(
        {
            catalog.accent_names.get(aid) or entry.get("name") or f"Accent {aid}"
            for aid, entry in catalog.accents.items()
            if any(catalog.voice_id(aid, g) for g in GENDERS)
        }
    )


def _area_code_from_context(call_context: Mapping[str, Any]) -> Optional[str]:
    override = str(call_context.get("accent_area_code") or "").strip()
    if _AREA_CODE_RE.match(override):
        return override
    try:
        number, _ = resolve_call_recipient(snapshot_call_parties(call_context))
    except RecipientResolutionError:
        return None
    return number[:3]


def resolve_accent_voice(
    voice_value: str,
    call_context: Optional[Mapping[str, Any]],
    *,
    catalog: Optional[AccentVoiceCatalog] = None,
) -> Optional[dict]:
    """Pick the accent voice for an ``auto:*`` voice value, or None to pass through.

    Returns ``{voice_id, accent_id, accent_name, gender, area_code, source}``.
    ``voice_id`` is "" when no voice is provisioned at all, so Fish falls back
    to its default voice. Never raises.
    """
    if voice_value not in AUTO_VOICE_VALUES:
        return None
    catalog = catalog or _catalog
    requested_gender = voice_value.split(":", 1)[1]

    try:
        area_code = _area_code_from_context(call_context or {})
    except Exception as e:  # never let voice routing break a call
        logger.warning(f"Accent area code lookup failed: {e}")
        area_code = None

    accent_id = catalog.area_code_to_accent.get(area_code) if area_code else None
    source = "area_code"
    if accent_id is None:
        accent_id = catalog.default_accent_id
        source = "default"

    other_gender = "male" if requested_gender == "female" else "female"
    candidates = [(accent_id, requested_gender), (accent_id, other_gender)]
    if accent_id != catalog.default_accent_id:
        candidates += [
            (catalog.default_accent_id, requested_gender),
            (catalog.default_accent_id, other_gender),
        ]

    for aid, gender in candidates:
        voice_id = catalog.voice_id(aid, gender)
        if voice_id:
            break
    else:
        aid, gender, voice_id = accent_id, requested_gender, ""

    if aid != accent_id:
        source = "default"

    return {
        "voice_id": voice_id,
        "accent_id": aid,
        "accent_name": catalog.accent_names.get(aid, ""),
        "gender": gender,
        "area_code": area_code,
        "source": source,
    }
