"""Local-accent Fish voices: one private instant clone per US regional accent.

``scripts/provision_accent_clones.py`` uploads each accent's approved reference
recording to Fish once and writes ``accent_clone_registry.json``
(accent_id -> fish_voice_id). Many area codes share one accent, and so one
clone: ``area_code_accent_map.json`` maps each area code to its accent_id.

Routing is switched on per agent (workflow) with the ``local_accent_voice``
workflow configuration, off by default. For an agent with it on and a Fish TTS,
``apply_local_accent_voice`` picks the clone from the prospect's area code when
the call's pipeline is built:

* inbound calls use ``caller_number``; outbound use ``called_number`` (or the
  legacy ``phone_number``) — never our own caller ID — via
  ``resolve_call_recipient``;
* toll-free, non-US, malformed, missing or unmapped numbers, and accents
  without a provisioned clone, use the default accent (68, Colorado / Central
  Rockies, the most neutral of the set);
* if the default accent has no clone either, the agent's own Fish voice is
  used (or the standard Fish voice when the agent has none).

``AUTO_LOCAL_ACCENT`` is a voice-picker value meaning "no fixed voice, always
the local accent". It still only routes on agents with the setting on; on any
other agent it falls back to the standard voice and is never sent to Fish.

Call startup only does lookups (phone number -> area code -> accent_id ->
reference_id); nothing is cloned in the call path. The data files are loaded
once on import; a missing registry is logged and leaves every auto call on the
fallback voice, it never breaks startup.
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
from api.services.voice_routing.blocked_voices import (
    DEFAULT_FISH_VOICE,
    is_blocked_fish_voice,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "accent_voices"
REGISTRY_FILE = "accent_clone_registry.json"
AREA_CODE_MAP_FILE = "area_code_accent_map.json"
DATASET_FILE = "accents.json"
PREVIEWS_DIR_NAME = "previews"

AUTO_LOCAL_ACCENT = "auto:local-accent"
AUTO_LOCAL_ACCENT_NAME = "Auto — Local Accent by Area Code"
FALLBACK_DEFAULT_ACCENT_ID = 68
STATUS_READY = "ready"

SOURCE_AREA_CODE = "area_code"
SOURCE_DEFAULT = "default"
SOURCE_STANDARD_VOICE = "standard_voice"
SOURCE_AGENT_VOICE = "agent_voice"

PREVIEW_ROUTE = "api/v1/public/accent-voices/previews"
PREVIEW_FILENAME_RE = re.compile(r"^\d{3}\.mp3$")

_AREA_CODE_RE = re.compile(r"^\d{3}$")
# NANP area codes start 2-9; N11 codes are service codes, not geography.
_GEOGRAPHIC_AREA_CODE_RE = re.compile(r"^[2-9](?!11)\d\d$")
TOLL_FREE_AREA_CODES = frozenset(
    {"800", "822", "833", "844", "855", "866", "877", "880", "887", "888", "889"}
)


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
        if not self.area_code_to_accent:
            logger.warning(
                f"Area code accent map missing or empty at "
                f"{self.data_dir / AREA_CODE_MAP_FILE}; every auto local-accent "
                "call will use the default accent."
            )

        # Names come from the dataset so an auto pick is labelled even when
        # that accent has no clone yet.
        self.accent_names: dict[int, str] = {}
        dataset = _read_json(self.data_dir / DATASET_FILE) or {}
        for acc in dataset.get("accents") or []:
            self.accent_names[int(acc["accent_id"])] = acc["name"]

        registry = _read_json(self.data_dir / REGISTRY_FILE)
        if registry is None:
            logger.warning(
                f"Accent clone registry not found at {self.data_dir / REGISTRY_FILE}; "
                "run scripts/provision_accent_clones.py. Auto local-accent calls "
                "use the standard Fish voice until it exists."
            )
            registry = {}
        self.registry: dict[int, dict] = {}
        for key, entry in registry.items():
            try:
                aid = int(entry.get("accent_id") or key)
            except (TypeError, ValueError, AttributeError):
                continue
            self.registry[aid] = entry
            if entry.get("accent_name"):
                self.accent_names.setdefault(aid, entry["accent_name"])

        try:
            self.preview_files = {
                p.name for p in self.previews_dir.iterdir() if p.is_file()
            }
        except OSError:
            self.preview_files = set()

    def voice_id(self, accent_id: Optional[int]) -> str:
        """The accent's Fish reference_id, or "" if it has no ready clone."""
        entry = self.registry.get(accent_id) if accent_id is not None else None
        if not entry or entry.get("status") != STATUS_READY:
            return ""
        return str(entry.get("fish_voice_id") or "")

    def ready_accent_ids(self) -> list[int]:
        return sorted(aid for aid in self.registry if self.voice_id(aid))

    def accent_name(self, accent_id: Optional[int]) -> str:
        return self.accent_names.get(accent_id, "") if accent_id is not None else ""

    def preview_filename(self, accent_id: int) -> Optional[str]:
        name = f"{accent_id:03d}.mp3"
        return name if name in self.preview_files else None


_catalog = AccentVoiceCatalog(DATA_DIR)

# O(1) lookups over the shipped data: AREA_CODE_TO_ACCENT["865"] == 21 and
# ACCENT_REGISTRY[21]["fish_voice_id"] is that accent's clone (once ready).
AREA_CODE_TO_ACCENT = _catalog.area_code_to_accent
ACCENT_REGISTRY = _catalog.registry


def get_catalog() -> AccentVoiceCatalog:
    return _catalog


def _auto_entry(catalog: AccentVoiceCatalog) -> dict:
    default_name = catalog.accent_name(catalog.default_accent_id) or "the default"
    ready = len(catalog.ready_accent_ids())
    return {
        "voice_id": AUTO_LOCAL_ACCENT,
        "name": AUTO_LOCAL_ACCENT_NAME,
        "accent": None,
        "language": "en-US",
        "description": (
            "Picks the cloned regional voice matching each prospect's area code "
            "when the call starts, on agents with Local Accent Voice turned on "
            "in their settings (others use the standard voice). Toll-free, "
            f"unknown or unmapped numbers use {default_name}. "
            f"{ready} regional voices provisioned."
        ),
    }


def _clone_entries(catalog: AccentVoiceCatalog, base_url: str) -> list[dict]:
    voices = []
    for aid in catalog.ready_accent_ids():
        entry = catalog.registry[aid]
        name = catalog.accent_name(aid) or f"Accent {aid}"
        states = entry.get("states") or []
        if isinstance(states, str):
            states = [states]
        area_codes = entry.get("area_codes") or []
        voice = {
            "voice_id": catalog.voice_id(aid),
            "name": name,
            "accent": name,
            "language": "en-US",
            "description": (
                f"{', '.join(states)} · area codes {', '.join(area_codes)}".strip(" ·")
            ),
        }
        preview = catalog.preview_filename(aid)
        if preview:
            voice["preview_url"] = f"{base_url}{PREVIEW_ROUTE}/{preview}"
        voices.append(voice)
    return voices


def list_fish_accent_voices(
    base_url: str,
    *,
    q: Optional[str] = None,
    accent: Optional[str] = None,
    catalog: Optional[AccentVoiceCatalog] = None,
) -> list[dict]:
    """VoiceInfo-shaped dicts: the Auto entry, then every provisioned clone.

    ``q`` is a case-insensitive substring match on name/accent/description;
    ``accent`` is a case-insensitive exact match. The Auto entry is always
    first, whatever the filters.
    """
    catalog = catalog or _catalog
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    q_lower = (q or "").strip().lower()
    accent_lower = (accent or "").strip().lower()

    def matches(voice: dict) -> bool:
        if q_lower and not any(
            q_lower in (voice.get(k) or "").lower()
            for k in ("name", "accent", "description")
        ):
            return False
        if accent_lower and (voice.get("accent") or "").lower() != accent_lower:
            return False
        return True

    return [_auto_entry(catalog)] + [
        v for v in _clone_entries(catalog, base_url) if matches(v)
    ]


def fish_accent_voice_accents(
    catalog: Optional[AccentVoiceCatalog] = None,
) -> list[str]:
    """Sorted accent names that have a provisioned clone."""
    catalog = catalog or _catalog
    return sorted(
        catalog.accent_name(aid) or f"Accent {aid}"
        for aid in catalog.ready_accent_ids()
    )


def _area_code_from_context(call_context: Mapping[str, Any]) -> Optional[str]:
    """The prospect's geographic US area code, or None if there isn't one."""
    override = str(call_context.get("accent_area_code") or "").strip()
    if _AREA_CODE_RE.match(override):
        return override
    try:
        number, _ = resolve_call_recipient(snapshot_call_parties(call_context))
    except RecipientResolutionError:
        return None
    area_code = number[:3]
    if area_code in TOLL_FREE_AREA_CODES or not _GEOGRAPHIC_AREA_CODE_RE.match(
        area_code
    ):
        return None
    return area_code


def resolve_accent_voice(
    voice_value: str,
    call_context: Optional[Mapping[str, Any]],
    *,
    fallback_voice: Optional[str] = None,
    catalog: Optional[AccentVoiceCatalog] = None,
) -> Optional[dict]:
    """Pick the clone for ``AUTO_LOCAL_ACCENT``, or None to pass the voice through.

    Returns ``{voice_id, accent_id, accent_name, area_code, source}`` where
    source is "area_code" (the prospect's own accent), "default" (accent 68),
    or, when no clone is usable (accent_id None), "agent_voice"
    (``fallback_voice``) or "standard_voice". ``voice_id`` is always a real
    Fish reference_id, never the auto value. Never raises.
    """
    if voice_value != AUTO_LOCAL_ACCENT:
        return None
    catalog = catalog or _catalog

    try:
        area_code = _area_code_from_context(call_context or {})
    except Exception as e:  # never let voice routing break a call
        logger.warning(f"Accent area code lookup failed: {e}")
        area_code = None

    accent_id = catalog.area_code_to_accent.get(area_code) if area_code else None
    source = SOURCE_AREA_CODE
    if accent_id is None or not catalog.voice_id(accent_id):
        accent_id = catalog.default_accent_id
        source = SOURCE_DEFAULT

    voice_id = catalog.voice_id(accent_id)
    if not voice_id:
        return {
            "voice_id": fallback_voice or DEFAULT_FISH_VOICE,
            "accent_id": None,
            "accent_name": "",
            "area_code": area_code,
            "source": SOURCE_AGENT_VOICE if fallback_voice else SOURCE_STANDARD_VOICE,
        }
    return {
        "voice_id": voice_id,
        "accent_id": accent_id,
        "accent_name": catalog.accent_name(accent_id),
        "area_code": area_code,
        "source": source,
    }


def local_accent_enabled(workflow_configurations: Optional[Mapping[str, Any]]) -> bool:
    """Whether an agent's workflow configuration turns local accent voices on."""
    setting = (workflow_configurations or {}).get("local_accent_voice")
    return isinstance(setting, Mapping) and setting.get("enabled") is True


def _with_voice(user_config: Any, voice: str) -> Any:
    tts = user_config.tts
    return user_config.model_copy(
        update={"tts": tts.model_copy(update={"voice": voice})}
    )


def apply_local_accent_voice(
    user_config: Any,
    call_context: dict,
    *,
    enabled: bool,
    workflow_run_id: Any = None,
    catalog: Optional[AccentVoiceCatalog] = None,
) -> tuple[Any, Optional[dict]]:
    """Give a Fish agent with local accents ``enabled`` this call's accent clone.

    Returns ``(user_config, pick)``. For non-Fish configs, and Fish agents with
    the setting off, ``pick`` is None and the voice is unchanged, except that
    an ``auto:local-accent`` voice becomes the standard voice. Otherwise the
    returned config is a copy whose ``tts.voice`` is the resolved reference_id
    (the agent's own voice is the fallback), and the pick is recorded on
    ``call_context`` as ``accent_voice`` and ``accent_name`` (so prompts can
    use ``{{accent_name}}``).
    """
    tts = getattr(user_config, "tts", None)
    if tts is None or getattr(tts, "provider", None) != "fish":
        return user_config, None
    voice = getattr(tts, "voice", None) or ""

    if not enabled:
        if voice == AUTO_LOCAL_ACCENT:
            logger.info(
                f"Local accent voice is off for workflow_run_id={workflow_run_id}; "
                f"using the standard voice instead of {AUTO_LOCAL_ACCENT}"
            )
            return _with_voice(user_config, DEFAULT_FISH_VOICE), None
        return user_config, None

    fallback = None
    if voice and voice != AUTO_LOCAL_ACCENT and not is_blocked_fish_voice(voice):
        fallback = voice
    pick = resolve_accent_voice(
        AUTO_LOCAL_ACCENT, call_context, fallback_voice=fallback, catalog=catalog
    )
    user_config = _with_voice(user_config, pick["voice_id"])
    call_context["accent_voice"] = dict(pick)
    call_context["accent_name"] = pick["accent_name"]
    # Area code only: the prospect's full number is never logged for routing.
    logger.info(
        "Accent voice resolved: "
        f"workflow_run_id={workflow_run_id} "
        f"area_code={pick['area_code']} "
        f"accent_id={pick['accent_id']} "
        f'accent_name="{pick["accent_name"]}" '
        f"source={pick['source']}"
    )
    return user_config, pick


def accent_runtime_configuration(pick: Optional[dict]) -> dict:
    """The routing decision as ``runtime_configuration`` fields (audit trail)."""
    if not pick:
        return {}
    return {
        "accent_id": pick["accent_id"],
        "accent_name": pick["accent_name"],
        "accent_area_code": pick["area_code"],
        "accent_source": pick["source"],
    }
