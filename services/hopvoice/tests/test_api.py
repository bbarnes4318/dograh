import json
from pathlib import Path

from fastapi.testclient import TestClient

from hopvoice.app import create_app
from hopvoice.config import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="stub",
        profile="native-fast",
        model_id="stub",
        device="cpu",
        dtype="float32",
        use_flash_attention=False,
        preload=False,
        api_key="secret",
        voices_file=str(tmp_path / "missing.json"),
        output_sample_rate=24000,
        audio_chunk_ms=20,
        max_concurrency=1,
        streaming_enabled=True,
    )


def test_openai_compatible_pcm_endpoint(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer secret"},
            json={
                "model": "native-fast",
                "input": "Hello there.",
                "voice": "test",
                "response_format": "pcm",
            },
        )
    assert response.status_code == 200
    assert response.headers["x-hopvoice-sample-rate"] == "24000"
    assert len(response.content) > 0
    assert len(response.content) % 2 == 0


def test_auth_is_enforced(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"model": "native-fast", "input": "Hello", "voice": "test"},
        )
    assert response.status_code == 401


def test_websocket_protocol_streams_binary_pcm(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/tts/ws", headers={"Authorization": "Bearer secret"}
        ) as ws:
            ws.send_json(
                {
                    "type": "start",
                    "protocol_version": 1,
                    "session_id": "call-1",
                    "model": "native-fast",
                    "voice_id": "test",
                    "output": {
                        "encoding": "pcm_s16le",
                        "sample_rate": 8000,
                        "channels": 1,
                    },
                }
            )
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["streaming_enabled"] is True
            assert ready["streaming_available"] is True

            ws.send_json({"type": "text", "turn_id": "turn-1", "text": "Hello."})
            assert ws.receive_json()["type"] == "turn_started"

            meta = ws.receive_json()
            assert meta["type"] == "audio_meta"
            assert meta["streaming"] is True
            assert meta["first_audio_ms"] is not None

            binary_frames = 0
            done = None
            while True:
                message = ws.receive()
                if message.get("bytes") is not None:
                    assert message["bytes"]
                    binary_frames += 1
                    continue
                payload = message.get("text")
                assert payload is not None
                event = json.loads(payload)
                if event["type"] == "turn_done":
                    done = event
                    break

            assert binary_frames > 0
            assert done["streaming"] is True
            assert done["generation_ms"] >= meta["first_audio_ms"]
            assert done["generated_audio_ms"] > 0
