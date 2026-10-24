from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class StyleRef:
    """An alternate reference clip of the *same* voice in a given delivery."""

    ref_audio: str
    ref_text: str | None = None


@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str
    language: str = "English"
    ref_audio: str | None = None
    ref_text: str | None = None
    x_vector_only_mode: bool = False
    speaker: str | None = None
    default_instruct: str | None = None
    # Delivery Planner style name -> reference clip. Missing styles use the
    # base ref_audio/ref_text above.
    styles: dict[str, StyleRef] = field(default_factory=dict)
    # Optional per-voice sampling defaults (fall back to service settings).
    temperature: float | None = None
    top_p: float | None = None


class VoiceRegistry:
    def __init__(self, path: str):
        self.path = Path(path)
        self._voices: dict[str, VoiceSpec] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._voices = {}
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        rows = payload.get("voices", payload)
        if not isinstance(rows, dict):
            raise ValueError(
                "voice registry must be an object or contain a 'voices' object"
            )

        base_dir = self.path.parent

        def resolve(path: str | None) -> str | None:
            # Relative reference paths are relative to voices.json, so one
            # registry works mounted at /voices and from a local checkout.
            if not path:
                return path
            candidate = Path(path)
            return str(candidate if candidate.is_absolute() else base_dir / candidate)

        voices: dict[str, VoiceSpec] = {}
        for voice_id, raw in rows.items():
            if not isinstance(raw, dict):
                raise ValueError(f"voice '{voice_id}' must be an object")
            voices[voice_id] = VoiceSpec(
                voice_id=voice_id,
                language=str(raw.get("language") or "English"),
                ref_audio=resolve(raw.get("ref_audio")),
                ref_text=raw.get("ref_text"),
                x_vector_only_mode=bool(raw.get("x_vector_only_mode", False)),
                speaker=raw.get("speaker"),
                default_instruct=raw.get("default_instruct"),
                styles={
                    str(name): StyleRef(
                        ref_audio=resolve(str(style["ref_audio"])),
                        ref_text=style.get("ref_text"),
                    )
                    for name, style in (raw.get("styles") or {}).items()
                    if isinstance(style, dict) and style.get("ref_audio")
                },
                temperature=(
                    float(raw["temperature"])
                    if raw.get("temperature") is not None
                    else None
                ),
                top_p=float(raw["top_p"]) if raw.get("top_p") is not None else None,
            )
        self._voices = voices

    def get(self, voice_id: str) -> VoiceSpec | None:
        return self._voices.get(voice_id)

    def ids(self) -> list[str]:
        return sorted(self._voices)

    def public_catalog(self) -> list[dict[str, str | bool | None]]:
        return [
            {
                "voice_id": spec.voice_id,
                "language": spec.language,
                "clone": bool(spec.ref_audio),
                "speaker": spec.speaker,
                "styles": sorted(spec.styles),
            }
            for spec in sorted(self._voices.values(), key=lambda item: item.voice_id)
        ]
