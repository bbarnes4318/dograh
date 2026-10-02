"""Authoritative OpenAI model / voice catalogs.

Every OpenAI audio surface Dograh exposes — chained-pipeline TTS, Realtime
speech-to-speech and GPT-Live — reads its models and voices from here:
backend validation, the config JSON schema (and therefore the UI) and the
runtime service factory. Do not repeat these strings elsewhere.

Sources (verified against OpenAI's public docs/announcements when this was
written): ``gpt-realtime-2`` (May 2026), ``gpt-live-1`` (API GA Sep 2026),
``gpt-4o-mini-tts``. OpenAI recommends ``marin`` and ``cedar`` for best
Realtime/TTS quality, so they are listed first.
"""

import re
from dataclasses import dataclass
from typing import Any

VOICE_TYPE_BUILTIN = "builtin"
VOICE_TYPE_CUSTOM = "custom"
OPENAI_VOICE_TYPES = (VOICE_TYPE_BUILTIN, VOICE_TYPE_CUSTOM)

# Custom voice ids look like ``voice_123abc``. Reject anything else so an
# arbitrary string never reaches the API as a voice object.
CUSTOM_VOICE_ID_PATTERN = re.compile(r"^voice_[A-Za-z0-9_-]{3,128}$")

CUSTOM_VOICE_UNAVAILABLE_MESSAGE = (
    "OpenAI custom voice is unavailable to this project or the configured "
    "voice ID is invalid."
)


@dataclass(frozen=True)
class OpenAIVoice:
    id: str
    label: str
    description: str
    group: str

    def as_dict(self) -> dict[str, str]:
        return {
            "value": self.id,
            "label": self.label,
            "description": self.description,
            "group": self.group,
        }


def _ids(voices: tuple[OpenAIVoice, ...]) -> tuple[str, ...]:
    return tuple(v.id for v in voices)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

OPENAI_LIVE_MODELS = ("gpt-live-1",)
OPENAI_LIVE_DEFAULT_MODEL = "gpt-live-1"

OPENAI_REALTIME_MODELS = ("gpt-realtime-2",)
OPENAI_REALTIME_DEFAULT_MODEL = "gpt-realtime-2"

OPENAI_TTS_MODELS = ("gpt-4o-mini-tts",)
OPENAI_TTS_DEFAULT_MODEL = "gpt-4o-mini-tts"

# --------------------------------------------------------------------------
# Voices
# --------------------------------------------------------------------------

_PREMIUM = "Recommended"
_LEGACY = "Classic"

_REALTIME_VOICE_DEFS = (
    OpenAIVoice("marin", "Marin", "Bright, clear, professional", _PREMIUM),
    OpenAIVoice("cedar", "Cedar", "Warm, natural, conversational", _PREMIUM),
    OpenAIVoice("alloy", "Alloy", "Neutral, balanced", _LEGACY),
    OpenAIVoice("ash", "Ash", "Confident, slightly husky", _LEGACY),
    OpenAIVoice("ballad", "Ballad", "Soft, melodic", _LEGACY),
    OpenAIVoice("coral", "Coral", "Friendly, upbeat", _LEGACY),
    OpenAIVoice("echo", "Echo", "Calm, steady", _LEGACY),
    OpenAIVoice("sage", "Sage", "Composed, measured", _LEGACY),
    OpenAIVoice("shimmer", "Shimmer", "Light, expressive", _LEGACY),
    OpenAIVoice("verse", "Verse", "Expressive, dynamic", _LEGACY),
)
OPENAI_REALTIME_VOICE_OPTIONS = _REALTIME_VOICE_DEFS
OPENAI_REALTIME_VOICES = _ids(_REALTIME_VOICE_DEFS)
OPENAI_REALTIME_DEFAULT_VOICE = "marin"

# Chat-completions-era TTS voices (fable/nova/onyx) are TTS-only.
_TTS_VOICE_DEFS = _REALTIME_VOICE_DEFS + (
    OpenAIVoice("fable", "Fable", "Storyteller, warm British", _LEGACY),
    OpenAIVoice("nova", "Nova", "Energetic, clear", _LEGACY),
    OpenAIVoice("onyx", "Onyx", "Deep, authoritative", _LEGACY),
)
OPENAI_TTS_VOICE_OPTIONS = _TTS_VOICE_DEFS
OPENAI_TTS_VOICES = _ids(_TTS_VOICE_DEFS)
OPENAI_TTS_DEFAULT_VOICE = "marin"

_NA = "North American"
_SOUTH = "Southern U.S."
_INTL = "British / Irish / Australian"
_OTHER = "Other"
_LIVE_VOICE_DEFS = (
    OpenAIVoice("gleam", "Gleam", "North American, feminine", _NA),
    OpenAIVoice("meridian", "Meridian", "North American, masculine", _NA),
    # marin is OpenAI's documented GPT-Live default voice.
    OpenAIVoice("marin", "Marin", "OpenAI default (American), feminine", _NA),
    OpenAIVoice("delta", "Delta", "Southern U.S., feminine", _SOUTH),
    OpenAIVoice("cinder", "Cinder", "Southern U.S., masculine", _SOUTH),
    OpenAIVoice("vesper", "Vesper", "British, masculine", _INTL),
    OpenAIVoice("willow", "Willow", "Irish, feminine", _INTL),
    OpenAIVoice("stone", "Stone", "Irish, masculine", _INTL),
    OpenAIVoice("quartz", "Quartz", "Australian, feminine", _INTL),
    OpenAIVoice("ripple", "Ripple", "Australian, masculine", _INTL),
    OpenAIVoice("beacon", "Beacon", "Filipino English, masculine", _OTHER),
    OpenAIVoice("bossa", "Bossa", "Brazilian Portuguese, feminine", _OTHER),
    OpenAIVoice("tempo", "Tempo", "Brazilian Portuguese, masculine", _OTHER),
)
OPENAI_LIVE_VOICE_OPTIONS = _LIVE_VOICE_DEFS
OPENAI_LIVE_VOICES = _ids(_LIVE_VOICE_DEFS)
OPENAI_LIVE_DEFAULT_VOICE = "meridian"

# Value for the JSON-schema ``voice_catalog`` extension the UI reads to render
# a grouped selector with descriptions.
OPENAI_REALTIME_VOICE_CATALOG = [v.as_dict() for v in OPENAI_REALTIME_VOICE_OPTIONS]
OPENAI_TTS_VOICE_CATALOG = [v.as_dict() for v in OPENAI_TTS_VOICE_OPTIONS]
OPENAI_LIVE_VOICE_CATALOG = [v.as_dict() for v in OPENAI_LIVE_VOICE_OPTIONS]

VOICE_INSTRUCTIONS_DESCRIPTION = (
    "Optional voice delivery instructions (accent, pace, tone). Applied to "
    "how the voice sounds only — keep workflow/business logic in your node "
    "prompts."
)


def validate_openai_voice(
    *,
    voice_type: str,
    voice: str,
    custom_voice_id: str | None,
    allowed_voices: tuple[str, ...],
    surface: str,
) -> None:
    """Raise ``ValueError`` if the voice selection is invalid for ``surface``."""
    if voice_type not in OPENAI_VOICE_TYPES:
        raise ValueError(
            f"voice_type must be one of {', '.join(OPENAI_VOICE_TYPES)}, got {voice_type!r}"
        )
    if voice_type == VOICE_TYPE_CUSTOM:
        if not custom_voice_id or not CUSTOM_VOICE_ID_PATTERN.match(custom_voice_id):
            raise ValueError(
                "custom_voice_id must look like 'voice_123abc' when voice_type is 'custom'"
            )
        return
    if voice not in allowed_voices:
        raise ValueError(
            f"{voice!r} is not a supported OpenAI {surface} voice "
            f"(supported: {', '.join(allowed_voices)})"
        )


def openai_voice_param(
    voice_type: str, voice: str, custom_voice_id: str | None
) -> str | dict[str, Any]:
    """Return the voice in the shape the OpenAI API expects.

    Built-in voices are plain strings; custom voices are ``{"id": ...}``
    objects — never a bare string.
    """
    if voice_type == VOICE_TYPE_CUSTOM:
        if not custom_voice_id:
            raise ValueError("custom_voice_id is required for a custom voice")
        return {"id": custom_voice_id}
    return voice
