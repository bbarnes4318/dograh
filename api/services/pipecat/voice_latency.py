"""Per-call latency marks for OpenAI voice sessions.

Collects monotonic timestamps for the phases that matter on a phone call and
logs one structured line per assistant turn. Only timings and ids are logged —
never audio, transcripts or credentials.
"""

import time
from typing import Callable

from loguru import logger

# Marks, in the order they normally occur within one turn.
SESSION_CONNECT_START = "session_connect_start"
SESSION_READY = "session_ready"
USER_SPEECH_STARTED = "user_speech_started"
USER_SPEECH_STOPPED = "user_speech_stopped"
TRANSCRIPT_COMPLETE = "transcript_complete"
BACKEND_START = "backend_start"
BACKEND_COMPLETE = "backend_complete"
FIRST_AUDIO_BYTE = "first_audio_byte"
FIRST_AUDIO_FRAME_SENT = "first_audio_frame_sent"
RESPONSE_COMPLETE = "response_complete"
INTERRUPTION_DETECTED = "interruption_detected"


class VoiceLatencyTracker:
    def __init__(
        self,
        *,
        component: str,
        call_id: str | int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._component = component
        self._call_id = call_id
        self._clock = clock
        self._marks: dict[str, float] = {}
        self._session_marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = self._clock()
        if name in (SESSION_CONNECT_START, SESSION_READY):
            self._session_marks[name] = now
            return
        self._marks[name] = now

    def delta_ms(self, start: str, end: str) -> float | None:
        a = self._marks.get(start, self._session_marks.get(start))
        b = self._marks.get(end, self._session_marks.get(end))
        if a is None or b is None or b < a:
            return None
        return round((b - a) * 1000, 1)

    def finish_turn(self) -> dict[str, float | None]:
        """Log and reset the per-turn marks; returns the computed latencies."""
        summary = {
            "session_connect_ms": self.delta_ms(SESSION_CONNECT_START, SESSION_READY),
            "speech_end_to_first_audio_ms": self.delta_ms(
                USER_SPEECH_STOPPED, FIRST_AUDIO_BYTE
            ),
            "speech_end_to_first_frame_sent_ms": self.delta_ms(
                USER_SPEECH_STOPPED, FIRST_AUDIO_FRAME_SENT
            ),
            "transcript_ms": self.delta_ms(USER_SPEECH_STOPPED, TRANSCRIPT_COMPLETE),
            "backend_ms": self.delta_ms(BACKEND_START, BACKEND_COMPLETE),
            "first_audio_to_response_complete_ms": self.delta_ms(
                FIRST_AUDIO_BYTE, RESPONSE_COMPLETE
            ),
            "interruption_detect_ms": self.delta_ms(
                USER_SPEECH_STARTED, INTERRUPTION_DETECTED
            ),
        }
        logger.info(
            f"voice_latency component={self._component} call_id={self._call_id} "
            + " ".join(f"{k}={v}" for k, v in summary.items() if v is not None)
        )
        self._marks.clear()
        return summary
