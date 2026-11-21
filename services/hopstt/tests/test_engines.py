import base64
import json
import subprocess
import sys
import threading

import numpy as np
import pytest

from conftest import make_settings, tone
from hopstt.engines import build_engine
from hopstt.engines.base import strip_language_tags
from hopstt.models import CATALOG


def test_strip_language_tags():
    assert strip_language_tags("Hello there. <en-US>") == ("Hello there.", "en")
    assert strip_language_tags("Hola. <es-ES> ¿Qué tal? <es-US>") == ("Hola. ¿Qué tal?", "es")
    assert strip_language_tags("no tag") == ("no tag", None)


def test_stub_engines_need_no_ml_imports():
    code = (
        "import sys; from hopstt.engines import build_engine; from hopstt.models import CATALOG;"
        "from hopstt.config import Settings;"
        "s=Settings(api_key='x'*20);"
        "e=build_engine(CATALOG['stub-streaming'], s); e.load(); e.warmup();"
        "b=build_engine(CATALOG['stub-batch'], s); b.load();"
        "import hopstt.server;"
        "print(any(m in sys.modules for m in ('torch','nemo','faster_whisper','qwen_asr')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_stub_streaming_is_deterministic():
    eng = build_engine(CATALOG["stub-streaming"], make_settings())
    eng.load()
    a, b = eng.new_state("en", 80), eng.new_state("auto", 80)
    audio = tone(0.8)
    for i in range(0, len(audio), 1280):
        ha, hb = eng.step([a, b], [audio[i : i + 1280]] * 2)
    assert ha.text == hb.text == "my date of birth"
    assert hb.language == "en"
    final = eng.finalize(a, np.zeros(0, np.float32))
    assert final.text == "My date of birth."
    assert eng.finalize(a, np.zeros(0, np.float32)).text == ""


# --------------------------------------------------------------------------
# vLLM realtime adapter against a protocol mock
# --------------------------------------------------------------------------
class FakeVllmRealtime:
    """Minimal stand-in for vLLM's /v1/realtime: echoes one word per append
    of non-silent audio as a delta; final commit -> transcription.done."""

    def __init__(self):
        from websockets.sync.server import serve

        self.events: list[dict] = []
        self.server = serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handler(self, ws):
        ws.send(json.dumps({"type": "session.created", "id": "sess-1", "created": 0}))
        text = ""
        generating = False
        for raw in ws:
            msg = json.loads(raw)
            self.events.append({k: v for k, v in msg.items() if k != "audio"})
            if msg["type"] == "input_audio_buffer.commit":
                if msg.get("final"):
                    ws.send(json.dumps({"type": "transcription.done", "text": text.strip(), "usage": None}))
                    text, generating = "", False
                else:
                    generating = True
            elif msg["type"] == "input_audio_buffer.append":
                assert generating, "append before commit"
                pcm = np.frombuffer(base64.b64decode(msg["audio"]), "<i2")
                if np.abs(pcm).max() > 1000:
                    text += " word"
                    ws.send(json.dumps({"type": "transcription.delta", "delta": " word"}))

    def close(self):
        self.server.shutdown()


def test_vllm_realtime_adapter_protocol():
    import time

    fake = FakeVllmRealtime()
    try:
        settings = make_settings(vllm_url=f"ws://127.0.0.1:{fake.port}/v1/realtime", voxtral_delay_ms=480)
        eng = build_engine(CATALOG["voxtral-realtime"], settings)
        eng.load()
        st = eng.new_state("en", 80)
        audio = tone(0.32)
        for i in range(0, len(audio), 1280):
            eng.step([st], [audio[i : i + 1280]])
        time.sleep(0.2)
        interim = eng.step([st], [np.zeros(1280, np.float32)])[0]
        assert interim.text == "word word word word"
        final = eng.finalize(st, np.zeros(0, np.float32))
        assert final.text == "word word word word" and final.language == "en"
        # next utterance on the same upstream connection
        eng.step([st], [tone(0.08)])
        assert eng.finalize(st, np.zeros(0, np.float32)).text == "word"
        eng.free(st)
        kinds = [e["type"] for e in fake.events]
        assert kinds[0] == "session.update" and fake.events[0]["model"] == settings.vllm_model
        assert kinds[1] == "input_audio_buffer.commit"
        assert {"type": "input_audio_buffer.commit", "final": True} in fake.events
    finally:
        fake.close()


def test_vllm_realtime_unreachable_is_engine_error():
    from hopstt.engines.base import EngineError

    eng = build_engine(CATALOG["voxtral-realtime"], make_settings(vllm_url="ws://127.0.0.1:9/v1/realtime"))
    with pytest.raises(EngineError):
        eng.load()
