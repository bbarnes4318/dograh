# Hopvoice — Hopwhistle Native Voice Engine

This directory is the first runnable slice of Hopwhistle's first-party neural
voice engine. It is **not** a Dograh SaaS/API integration. It is an internal GPU
service intended to run beside Hopwhistle's self-hosted AI Voice runtime.

## What milestone 0.2 does

- owns a stable Hopwhistle model profile (`native-fast` by default);
- loads Qwen3-TTS locally on our GPU (`0.6B Base` by default);
- caches licensed voice-clone prompts so reference features are not rebuilt on
  every utterance;
- exposes an OpenAI-compatible streaming HTTP endpoint that the existing
  `Speaches` Pipecat adapter can call **today**, without changing the live
  provider registry;
- exposes the long-term persistent WebSocket protocol at `/v1/tts/ws` with
  `start`, `text`, binary PCM, `cancel_turn`, `cancel_all`, and `end`;
- serializes multiple TTS chunks per call so sentence-level LLM streaming cannot
  overlap model inference or produce out-of-order audio;
- marks both active and queued speech stale on barge-in so an interrupted caller
  never hears a delayed sentence afterward;
- keeps the GPU concurrency semaphore held until a blocking model call truly
  exits, even when its caller-facing task has been cancelled;
- outputs raw signed 16-bit PCM and can render 8/16/24/48 kHz on the WebSocket
  path;
- includes auth, concurrency limiting, health, a voice catalog, and minimal
  Prometheus metrics;
- includes a `stub` backend so protocol/API tests run without a GPU or model
  weights;
- includes `api/services/pipecat/hopvoice_tts.py`, the persistent native Pipecat
  adapter that will replace the temporary HTTP compatibility path after provider
  registry/factory wiring.

## Important limitation of this first slice

The public `qwen-tts==0.1.1` Python wrapper returns a complete waveform. Its own
source documents that `non_streaming_mode=False` only simulates streaming text
input and does **not** enable true streaming generation. Therefore v0.2 has a
streaming network contract but the Qwen backend does not yet achieve our TTFA
objective.

Cancellation drops unsent audio immediately. If the current public Qwen wrapper
is already inside blocking GPU generation, that underlying thread cannot be
force-stopped yet. Hopvoice keeps the GPU semaphore held until it exits so a
fresh caller response can never overlap stale generation on the same worker.

That is deliberate. We first lock the service boundary, voice provenance,
telephony rendering, metrics, interruption semantics, and runtime integration.
The next engine task is lower-level token/decoder streaming and a cancellable
generation loop behind the same contract.

## Run a no-GPU smoke test

```bash
cd services/hopvoice
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
HOPVOICE_BACKEND=stub HOPVOICE_PRELOAD=0 \
  uvicorn hopvoice.app:app --host 0.0.0.0 --port 8787
```

Then:

```bash
curl http://127.0.0.1:8787/healthz
curl -o out.pcm \
  -H 'Authorization: Bearer dev-secret' \
  -H 'Content-Type: application/json' \
  -d '{"model":"native-fast","input":"Hello there.","voice":"test","response_format":"pcm"}' \
  http://127.0.0.1:8787/v1/audio/speech
```

Set `HOPVOICE_API_KEY=dev-secret` if you want auth enabled in local testing.

## GPU/Qwen run

Build and run with NVIDIA Container Toolkit:

```bash
docker build -t hopvoice:0.2 services/hopvoice

docker run --rm --gpus all -p 8787:8787 \
  -e HOPVOICE_API_KEY='replace-me' \
  -e HOPVOICE_PROFILE=native-fast \
  -e HOPVOICE_MODEL_ID=Qwen/Qwen3-TTS-12Hz-0.6B-Base \
  -e HOPVOICE_VOICES_FILE=/voices/voices.json \
  -v /opt/hopvoice/voices:/voices:ro \
  -v /opt/hopvoice/hf-cache:/home/hopvoice/.cache/huggingface \
  hopvoice:0.2
```

For a premium worker, run a separate container/GPU allocation with
`HOPVOICE_PROFILE=native-premium` and
`Qwen/Qwen3-TTS-12Hz-1.7B-Base`. Keeping one profile per worker prevents two
large checkpoints from unexpectedly occupying the same GPU.

## Voice registry

Copy `voices.example.json` to the mounted voice directory. `ref_audio` must be a
licensed/consented voice reference and `ref_text` must match it exactly for the
normal ICL clone mode.

No public figure, commercial actor, customer, or employee voice should be added
without explicit synthetic-voice/model-training rights.

## Existing runtime compatibility path

Until `api/services/pipecat/hopvoice_tts.py` is wired into the provider
registry/factory, the existing `Speaches` TTS provider can reach Hopvoice through
`/v1/audio/speech`:

- provider: `speaches`
- base URL: `http://<hopvoice-host>:8787/v1`
- model: `native-fast`
- voice: your Hopvoice `voice_id`
- API key: `HOPVOICE_API_KEY` (or `none` only on an isolated network with auth disabled)

This path is for first audio/quality benchmarks. The persistent WebSocket path is
the production target because it enables context continuity, server-side
cancellation, future acoustic memory, and lower per-turn overhead.

## WebSocket protocol v1

Hopvoice v0.2 serializes multiple queued text chunks per call session. This is
important because Pipecat may produce more than one sentence/chunk from a single
LLM response. A `cancel_all` command marks the active turn and every queued turn
stale, suppresses their audio, and is the command the native Pipecat adapter uses
on caller interruption.

Client start:

```json
{"type":"start","protocol_version":1,"session_id":"call-uuid","model":"native-fast","voice_id":"alex_licensed_v1","output":{"encoding":"pcm_s16le","sample_rate":8000,"channels":1}}
```

The server answers `ready`. A turn is:

```json
{"type":"text","turn_id":"turn-17","text":"Okay, that makes sense."}
```

The server sends `turn_started`, `audio_meta`, binary PCM frames, then
`turn_done`. Interrupt a single turn with:

```json
{"type":"cancel_turn","turn_id":"turn-17"}
```

Use `{"type":"cancel_all"}` on barge-in so no queued sentence can speak after
the interruption, and finish the call with `{"type":"end"}`.

## Next engineering gates

1. Replace complete-waveform Qwen generation with true token/decoder streaming.
2. Make cancellation stop GPU generation, not merely suppress its output.
3. Wire the now-implemented `api/services/pipecat/hopvoice_tts.py` adapter into
   provider configuration/factory and deploy it behind a feature flag.
4. Add Delivery Planner controls (speech act, emotion, pace, energy, emphasis).
5. Add call-session acoustic continuity.
6. Benchmark native 24 kHz -> G.711 8 kHz vs direct low-rate generation.
7. Gate production on blind human-vs-AI phone tests plus TTFA/cancel-tail metrics.
