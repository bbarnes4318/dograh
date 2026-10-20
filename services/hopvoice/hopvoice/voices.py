from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str
    language: str = "English"
    ref_audio: str | None = None
    ref_text: str | None = None
    x_vector_only_mode: bool = False
    speaker: str | None = None
    default_instruct: str | None = None


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
            raise ValueError("voice registry must be an object or contain a 'voices' object")

        voices: dict[str, VoiceSpec] = {}
        for voice_id, raw in rows.items():
            if not isinstance(raw, dict):
                raise ValueError(f"voice '{voice_id}' must be an object")
            voices[voice_id] = VoiceSpec(
                voice_id=voice_id,
                language=str(raw.get("language") or "English"),
                ref_audio=raw.get("ref_audio"),
                ref_text=raw.get("ref_text"),
                x_vector_only_mode=bool(raw.get("x_vector_only_mode", False)),
                speaker=raw.get("speaker"),
                default_instruct=raw.get("default_instruct"),
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
            }
            for spec in sorted(self._voices.values(), key=lambda item: item.voice_id)
        ]
