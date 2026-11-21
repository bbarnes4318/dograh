"""Nemotron streaming through NVIDIA NeMo-Speech.cpp (ggml, CPU or CUDA).

NeMo-Speech.cpp (https://github.com/NVIDIA/NeMo-Speech.cpp, Apache-2.0) runs
the official ``*.q8_0.gguf`` weights shipped in the nvidia Nemotron repos. Its
``nemo-speech serve`` binary exposes a realtime WebSocket
(``/v1/audio/transcriptions/realtime``, docs/api.md; semantics checked in
server/http/http_server.cpp at v0.3.0):

  server -> {"type":"session.created", ...}
  client -> {"type":"session.update","session":{"sample_rate":16000,"language":..}}
  client -> binary PCM16 LE mono frames
  server -> {"type":"conversation.item.input_audio_transcription.delta","delta":..}
  client -> {"type":"input_audio_buffer.commit"}
  server -> {"type":"conversation.item.input_audio_transcription.completed","transcript":..}
            (zero or more), then {"type":"input_audio_buffer.committed"}
  client -> more binary audio: the server starts a fresh stream

HopSTT keeps its own protocol, auth, admission, G.711 decoding, resampling and
metrics; this adapter only forwards each session's 16 kHz audio to one upstream
connection and maps ``finalize`` to ``commit``. The chunk size is a server-wide
setting of NeMo-Speech.cpp (``asr.streaming.rnnt_right_context``), so the model
spec advertises the single chunk size the sidecar was started with
(``HOPSTT_NEMO_CPP_CHUNK_MS``).

Delta semantics: the server sends the new suffix when the partial extends the
previous one, otherwise the whole new partial. Both arrive in the same field, so
a rewrite is recognised when the delta repeats the start of the current partial.
Finals always carry the full transcript, so this only ever affects interims.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

import numpy as np

from .base import EngineError, Hypothesis, StreamingEngine

logger = logging.getLogger("hopstt.nemo_cpp")
COMMIT_TIMEOUT_S = 10.0
DELTA = "conversation.item.input_audio_transcription.delta"
COMPLETED = "conversation.item.input_audio_transcription.completed"

# NeMo-Speech.cpp language codes for the prompt-conditioned Nemotron 3.5 model.
LANGUAGE_CODES = {"en": "en-US", "es": "es-US", "auto": "auto"}


@dataclass
class CppState:
    language: str
    chunk_ms: int
    conn: object = None
    reader: threading.Thread | None = None
    partial: str = ""
    finals: list[str] = field(default_factory=list)
    committed: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False


def _pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()


def apply_delta(previous: str, delta: str) -> str:
    """Rebuild the current partial from a NeMo-Speech.cpp delta event."""
    if not previous.strip():
        return delta
    head = _word_key(previous.split(maxsplit=1)[0])
    # An extension is a suffix: it starts with a space (new word) or continues
    # the last word. A rewrite is the whole new partial, so it starts with the
    # partial's first word (punctuation/capitalisation may change) and no space.
    if head and not delta[:1].isspace() and delta.split() and _word_key(delta.split()[0]) == head:
        return delta
    return previous + delta


def _word_key(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum())


class NemoSpeechCppEngine(StreamingEngine):
    name = "nemo-speech-cpp"

    def load(self) -> None:
        try:
            from websockets.sync.client import connect  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise EngineError("websockets is required for nemo_speech_cpp", transient=False) from exc
        state = self.new_state(self.spec.languages[0], self._chunk_ms())
        self.free(state)
        self.loaded = True

    def warmup(self) -> None:
        state = self.new_state(self.spec.languages[0], self._chunk_ms())
        try:
            chunk = np.zeros(16 * state.chunk_ms, np.float32)
            self.step([state], [chunk])
            self.finalize(state, np.zeros(0, np.float32))
        finally:
            self.free(state)
        self.warm = True

    def _chunk_ms(self) -> int:
        return int(self.spec.default_chunk_ms or (self.spec.chunk_ms or (160,))[0])

    # ------------------------------------------------------------------
    def _send(self, state: CppState, payload) -> None:
        try:
            state.conn.send(payload)
        except Exception as exc:
            raise EngineError(f"nemo-speech send failed: {type(exc).__name__}") from exc

    def _read_loop(self, state: CppState) -> None:
        try:
            for raw in state.conn:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == DELTA:
                    with state.lock:
                        state.partial = apply_delta(state.partial, msg.get("delta", ""))
                elif kind == COMPLETED:
                    with state.lock:
                        text = (msg.get("transcript") or "").strip()
                        if text:
                            state.finals.append(text)
                        state.partial = ""
                elif kind == "input_audio_buffer.committed":
                    state.committed.set()
                elif kind == "error":
                    err = msg.get("error") or {}
                    state.error = str(err.get("code") or err.get("type") or "error")
                    state.committed.set()
        except Exception as exc:
            if not state.closed:
                state.error = type(exc).__name__
                state.committed.set()

    def new_state(self, language: str, chunk_ms: int) -> CppState:
        from websockets.sync.client import connect

        state = CppState(language=language, chunk_ms=chunk_ms)
        headers = {}
        if self.settings.nemo_cpp_api_key:
            headers["Authorization"] = f"Bearer {self.settings.nemo_cpp_api_key}"
        session = {"sample_rate": 16000, "automatic_punctuation": True}
        code = LANGUAGE_CODES.get(language)
        if code and code != "auto":
            session["language"] = code
        try:
            state.conn = connect(
                self.settings.nemo_cpp_url,
                open_timeout=10,
                max_size=None,
                additional_headers=headers or None,
            )
            hello = json.loads(state.conn.recv(timeout=10))
            if hello.get("type") != "session.created":
                raise EngineError("unexpected nemo-speech greeting")
            state.conn.send(json.dumps({"type": "session.update", "session": session}))
            reply = json.loads(state.conn.recv(timeout=10))
            if reply.get("type") == "error":
                err = reply.get("error") or {}
                raise EngineError(f"nemo-speech rejected session: {err.get('code')}", transient=False)
        except EngineError:
            self._close(state)
            raise
        except Exception as exc:
            self._close(state)
            raise EngineError(f"nemo-speech connect failed: {type(exc).__name__}") from exc
        state.reader = threading.Thread(target=self._read_loop, args=(state,), daemon=True)
        state.reader.start()
        return state

    def _hyp(self, state: CppState, text: str) -> Hypothesis:
        return Hypothesis(text=" ".join(text.split()), language=state.language)

    def step(self, states, chunks):
        out = []
        for state, chunk in zip(states, chunks):
            if state.error:
                raise EngineError(f"nemo-speech session error: {state.error}")
            if len(chunk):
                self._send(state, _pcm16(chunk))
            with state.lock:
                out.append(self._hyp(state, state.partial))
        return out

    def finalize(self, state: CppState, tail: np.ndarray) -> Hypothesis:
        if len(tail):
            self._send(state, _pcm16(tail.astype(np.float32)))
        state.committed.clear()
        self._send(state, json.dumps({"type": "input_audio_buffer.commit"}))
        if not state.committed.wait(COMMIT_TIMEOUT_S):
            raise EngineError("nemo-speech did not commit the utterance in time")
        if state.error:
            raise EngineError(f"nemo-speech session error: {state.error}")
        with state.lock:
            text = " ".join(state.finals)
            state.finals.clear()
            state.partial = ""
        return self._hyp(state, text)

    def reset_utterance(self, state: CppState) -> None:
        with state.lock:
            state.partial = ""
            state.finals.clear()
        try:
            self._send(state, json.dumps({"type": "input_audio_buffer.clear"}))
        except EngineError:
            pass

    def _close(self, state: CppState) -> None:
        state.closed = True
        if state.conn is not None:
            try:
                state.conn.close()
            except Exception:
                pass

    def free(self, state: CppState) -> None:
        self._close(state)
        if state.reader is not None:
            state.reader.join(timeout=2)
