"""Voxtral Mini 4B Realtime via a separately-run vLLM server.

vLLM's realtime endpoint (``/v1/realtime``, see
``vllm/entrypoints/speech_to_text/realtime/connection.py``) speaks:

  server -> {"type":"session.created"}
  client -> {"type":"session.update","model":...}
  client -> {"type":"input_audio_buffer.commit"}          start a generation
  client -> {"type":"input_audio_buffer.append","audio":b64(PCM16 16 kHz)}
  server -> {"type":"transcription.delta","delta":...}
  client -> {"type":"input_audio_buffer.commit","final":true}  end of input
  server -> {"type":"transcription.done","text":...}

Each HopSTT session holds one upstream connection. ``step()`` forwards the
chunk and returns the text accumulated from deltas; ``finalize()`` appends
``transcription_delay_ms`` + 160 ms of silence (the model emits text that far
behind the audio), commits ``final``, waits for ``transcription.done``, and
opens the next generation. The model's delay itself is configured on the
vLLM server (``transcription_delay_ms`` in tekken.json, multiple of 80,
480 recommended); ``HOPSTT_VOXTRAL_DELAY_MS`` must match it. GPU-only;
covered here by a protocol mock test.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from dataclasses import dataclass, field

import numpy as np

from .base import EngineError, Hypothesis, StreamingEngine

logger = logging.getLogger("hopstt.vllm")
DONE_TIMEOUT_S = 10.0


@dataclass
class VoxtralState:
    language: str
    chunk_ms: int
    conn: object = None
    reader: threading.Thread | None = None
    text: str = ""
    done_text: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False


def _pcm16_b64(audio: np.ndarray) -> str:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
    return base64.b64encode(pcm).decode("ascii")


class VllmRealtimeEngine(StreamingEngine):
    name = "vllm-realtime"

    def load(self) -> None:
        try:
            from websockets.sync.client import connect  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise EngineError("websockets is required for vllm_realtime", transient=False) from exc
        # Fail fast if the vLLM server is unreachable or lacks the model.
        state = self.new_state(self.spec.languages[0], self.spec.default_chunk_ms or 80)
        self.free(state)
        self.loaded = True

    def warmup(self) -> None:
        state = self.new_state(self.spec.languages[0], self.spec.default_chunk_ms or 80)
        try:
            self.step([state], [np.zeros(16 * state.chunk_ms, np.float32)])
            self.finalize(state, np.zeros(0, np.float32))
        finally:
            self.free(state)
        self.warm = True

    # ------------------------------------------------------------------
    def _send(self, state: VoxtralState, message: dict) -> None:
        try:
            state.conn.send(json.dumps(message))
        except Exception as exc:
            raise EngineError(f"vllm send failed: {type(exc).__name__}") from exc

    def _read_loop(self, state: VoxtralState) -> None:
        try:
            for raw in state.conn:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "transcription.delta":
                    with state.lock:
                        state.text += msg.get("delta", "")
                elif kind == "transcription.done":
                    with state.lock:
                        state.done_text = msg.get("text", state.text)
                    state.done.set()
                elif kind == "error":
                    state.error = str(msg.get("code") or "error")
                    state.done.set()
        except Exception as exc:
            if not state.closed:
                state.error = type(exc).__name__
                state.done.set()

    def new_state(self, language: str, chunk_ms: int) -> VoxtralState:
        from websockets.sync.client import connect

        state = VoxtralState(language=language, chunk_ms=chunk_ms)
        try:
            state.conn = connect(self.settings.vllm_url, open_timeout=10, max_size=None)
            hello = json.loads(state.conn.recv(timeout=10))
            if hello.get("type") != "session.created":
                raise EngineError("unexpected vLLM greeting")
            state.conn.send(json.dumps({"type": "session.update", "model": self.settings.vllm_model}))
            state.conn.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except EngineError:
            self._close(state)
            raise
        except Exception as exc:
            self._close(state)
            raise EngineError(f"vLLM connect failed: {type(exc).__name__}") from exc
        state.reader = threading.Thread(target=self._read_loop, args=(state,), daemon=True)
        state.reader.start()
        return state

    def _hyp(self, state: VoxtralState, text: str) -> Hypothesis:
        language = state.language
        return Hypothesis(text=" ".join(text.split()), language=language)

    def step(self, states, chunks):
        out = []
        for state, chunk in zip(states, chunks):
            if state.error:
                raise EngineError(f"vLLM session error: {state.error}")
            self._send(state, {"type": "input_audio_buffer.append", "audio": _pcm16_b64(chunk)})
            with state.lock:
                out.append(self._hyp(state, state.text))
        return out

    def finalize(self, state: VoxtralState, tail: np.ndarray) -> Hypothesis:
        pad_ms = int(self.settings.voxtral_delay_ms) + 160
        audio = np.concatenate([tail.astype(np.float32), np.zeros(16 * pad_ms, np.float32)])
        state.done.clear()
        self._send(state, {"type": "input_audio_buffer.append", "audio": _pcm16_b64(audio)})
        self._send(state, {"type": "input_audio_buffer.commit", "final": True})
        if not state.done.wait(DONE_TIMEOUT_S):
            raise EngineError("vLLM did not finish the utterance in time")
        if state.error:
            raise EngineError(f"vLLM session error: {state.error}")
        with state.lock:
            text = state.done_text if state.done_text is not None else state.text
            state.text = ""
            state.done_text = None
        # Next utterance: a new generation on the same connection.
        self._send(state, {"type": "input_audio_buffer.commit"})
        return self._hyp(state, text)

    def reset_utterance(self, state: VoxtralState) -> None:
        with state.lock:
            state.text = ""

    def _close(self, state: VoxtralState) -> None:
        state.closed = True
        if state.conn is not None:
            try:
                state.conn.close()
            except Exception:
                pass

    def free(self, state: VoxtralState) -> None:
        self._close(state)
        if state.reader is not None:
            state.reader.join(timeout=2)
