"""nemo_speech_cpp adapter against a fake NeMo-Speech.cpp realtime server.

The fake follows docs/api.md and server/http/http_server.cpp at v0.3.0:
session.created on connect, one session.update -> session.updated, binary PCM16
frames, delta events (suffix when the partial extends, whole partial on a
rewrite), commit -> completed (if any text) + input_audio_buffer.committed and a
fresh stream for the next audio.
"""

import json
import threading
import time

import numpy as np
import pytest

from conftest import make_settings, tone
from hopstt.engines import build_engine
from hopstt.engines.base import EngineError
from hopstt.engines.nemo_speech_cpp import apply_delta
from hopstt.models import CATALOG

DELTA = "conversation.item.input_audio_transcription.delta"
COMPLETED = "conversation.item.input_audio_transcription.completed"


class FakeNemoSpeech:
    def __init__(self, api_key: str | None = "k" * 20, rewrite_at: int | None = None):
        from websockets.sync.server import serve

        self.api_key = api_key
        self.rewrite_at = rewrite_at
        self.sessions: list[dict] = []
        self.events: list[str] = []
        self.server = serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/v1/audio/transcriptions/realtime"

    def handler(self, ws):
        if self.api_key and ws.request.headers.get("Authorization") != f"Bearer {self.api_key}":
            ws.send(json.dumps({"type": "error", "error": {"message": "unauthorized",
                                                            "type": "authentication_error", "code": "unauthorized"}}))
            ws.close()
            return
        ws.send(json.dumps({"type": "session.created", "session": {"input_audio_format": "pcm16",
                                                                   "sample_rate": 16000}}))
        partial, words = "", 0
        for raw in ws:
            if isinstance(raw, bytes):
                pcm = np.frombuffer(raw, "<i2")
                if len(pcm) and np.abs(pcm).max() > 1000:
                    words += 1
                    new = " ".join(["word"] * words)
                    if self.rewrite_at and words == self.rewrite_at:
                        new = " ".join(["Word"] + ["word"] * (words - 1))
                    delta = new[len(partial):] if new.startswith(partial) else new
                    partial = new
                    ws.send(json.dumps({"type": DELTA, "delta": delta, "audio_processed": 0.1}))
                continue
            msg = json.loads(raw)
            self.events.append(msg["type"])
            if msg["type"] == "session.update":
                self.sessions.append(msg["session"])
                ws.send(json.dumps({"type": "session.updated", "session": msg["session"]}))
            elif msg["type"] == "input_audio_buffer.commit":
                if partial:
                    ws.send(json.dumps({"type": COMPLETED, "transcript": partial + "."}))
                ws.send(json.dumps({"type": "input_audio_buffer.committed"}))
                partial, words = "", 0
            elif msg["type"] == "input_audio_buffer.clear":
                partial, words = "", 0
                ws.send(json.dumps({"type": "input_audio_buffer.cleared"}))

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = FakeNemoSpeech()
    yield f
    f.close()


def _engine(fake, **kw):
    settings = make_settings(nemo_cpp_url=fake.url, nemo_cpp_api_key=fake.api_key, **kw)
    return build_engine(CATALOG["nemotron-3.5-streaming-cpp"], settings)


def _feed(eng, st, audio, step=2560):
    hyp = None
    for i in range(0, len(audio), step):
        hyp = eng.step([st], [audio[i : i + step]])[0]
    return hyp


def test_apply_delta_suffix_and_rewrite():
    assert apply_delta("", "my") == "my"
    assert apply_delta("my", " date") == "my date"
    assert apply_delta("my da", "te of") == "my date of"
    # rewrite: the whole partial again, starting with the first word
    assert apply_delta("my date of", "my data of birth") == "my data of birth"


def test_partial_final_and_next_utterance(fake):
    eng = _engine(fake)
    eng.load()
    st = eng.new_state("es", 160)
    _feed(eng, st, tone(0.48))
    time.sleep(0.2)
    interim = eng.step([st], [np.zeros(2560, np.float32)])[0]
    assert interim.text == "word word word" and interim.language == "es"
    final = eng.finalize(st, np.zeros(0, np.float32))
    assert final.text == "word word word."
    # same upstream connection, fresh stream after commit
    _feed(eng, st, tone(0.16))
    assert eng.finalize(st, np.zeros(0, np.float32)).text == "word."
    eng.free(st)
    # language mapped to the NeMo-Speech.cpp code, 16 kHz PCM forwarded
    assert fake.sessions[-1]["language"] == "es-US" and fake.sessions[-1]["sample_rate"] == 16000


def test_rewrite_delta_is_not_duplicated(fake):
    fake.rewrite_at = 3
    eng = _engine(fake)
    st = eng.new_state("en", 160)
    _feed(eng, st, tone(0.48))
    time.sleep(0.2)
    assert eng.step([st], [np.zeros(2560, np.float32)])[0].text == "Word word word"
    eng.free(st)


def test_finalize_without_speech_gives_empty_final(fake):
    eng = _engine(fake)
    st = eng.new_state("en", 160)
    _feed(eng, st, np.zeros(16000, np.float32))
    assert eng.finalize(st, np.zeros(0, np.float32)).text == ""
    eng.free(st)


def test_auto_language_sends_no_language_code(fake):
    eng = _engine(fake)
    st = eng.new_state("auto", 160)
    eng.free(st)
    assert "language" not in fake.sessions[-1]


def test_bad_key_is_engine_error(fake):
    settings = make_settings(nemo_cpp_url=fake.url, nemo_cpp_api_key="wrong-key-wrong-key")
    eng = build_engine(CATALOG["nemotron-3.5-streaming-cpp"], settings)
    with pytest.raises(EngineError):
        eng.load()


def test_unreachable_is_engine_error():
    settings = make_settings(nemo_cpp_url="ws://127.0.0.1:9/v1/audio/transcriptions/realtime")
    eng = build_engine(CATALOG["nemotron-3.5-streaming-cpp"], settings)
    with pytest.raises(EngineError):
        eng.load()


def test_spec_advertises_single_server_chunk():
    spec = CATALOG["nemotron-3.5-streaming-cpp"]
    assert spec.chunk_ms == (spec.default_chunk_ms,)
    assert spec.options["rnnt_right_context"] in (0, 1, 3, 6, 13)
    assert CATALOG["nemotron-en-streaming-cpp"].languages == ("en",)
