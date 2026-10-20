import json
import time as time_module

from fastapi.testclient import TestClient

from hopvoice.app import create_app

from test_api import settings


def _recv_events_until(ws, target_type: str, target_turn: str | None = None):
    events = []
    binary_frames = 0
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            binary_frames += 1
            continue
        text = message.get("text")
        assert text is not None
        event = json.loads(text)
        events.append(event)
        if event.get("type") == target_type and (
            target_turn is None or event.get("turn_id") == target_turn
        ):
            return events, binary_frames


def _start(ws, session_id: str):
    ws.send_json(
        {
            "type": "start",
            "session_id": session_id,
            "model": "native-fast",
            "voice_id": "test",
            "output": {"encoding": "pcm_s16le", "sample_rate": 8000, "channels": 1},
        }
    )
    assert ws.receive_json()["type"] == "ready"


def test_websocket_queues_multiple_tts_chunks_in_order(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/tts/ws", headers={"Authorization": "Bearer secret"}
        ) as ws:
            _start(ws, "call-queue")
            ws.send_json({"type": "text", "turn_id": "a", "text": "First sentence."})
            ws.send_json({"type": "text", "turn_id": "b", "text": "Second sentence."})

            events, binary_frames = _recv_events_until(ws, "turn_done", "b")
            types = [(e.get("type"), e.get("turn_id")) for e in events]
            assert types.index(("turn_started", "a")) < types.index(("turn_done", "a"))
            assert types.index(("turn_done", "a")) < types.index(("turn_started", "b"))
            assert types.index(("turn_started", "b")) < types.index(("turn_done", "b"))
            assert binary_frames > 0


def test_cancel_all_drops_active_and_queued_speech(tmp_path, monkeypatch):
    app = create_app(settings(tmp_path))
    original = app.state.backend.synthesize

    def slow_synthesize(**kwargs):
        time_module.sleep(0.08)
        return original(**kwargs)

    monkeypatch.setattr(app.state.backend, "synthesize", slow_synthesize)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/tts/ws", headers={"Authorization": "Bearer secret"}
        ) as ws:
            _start(ws, "call-cancel")
            ws.send_json({"type": "text", "turn_id": "old-active", "text": "Old active."})
            ws.send_json({"type": "text", "turn_id": "old-queued", "text": "Old queued."})

            while True:
                event = ws.receive_json()
                if event.get("type") == "turn_started" and event.get("turn_id") == "old-active":
                    break

            ws.send_json({"type": "cancel_all"})
            while True:
                event = ws.receive_json()
                if event.get("type") == "cancelled_all":
                    assert event["count"] >= 1
                    break

            ws.send_json({"type": "text", "turn_id": "fresh", "text": "Fresh response."})
            events, _ = _recv_events_until(ws, "turn_done", "fresh")
            assert not any(
                e.get("type") == "turn_started" and e.get("turn_id") == "old-queued"
                for e in events
            )
            assert not any(
                e.get("type") == "turn_done" and e.get("turn_id") == "old-active"
                for e in events
            )
