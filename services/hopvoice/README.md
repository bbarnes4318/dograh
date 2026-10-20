# Hopvoice — Hopwhistle Native Voice Engine

This directory is the first runnable slice of Hopwhistle's first-party neural
voice engine. It is **not** a Dograh SaaS/API integration. It is an internal GPU
service intended to run beside Hopwhistle's self-hosted AI Voice runtime.

## What milestone 0.1 does

- owns a stable Hopwhistle model profile (`native-fast` by default);
- loads Qwen3-TTS locally on our GPU (`0.6B Base` by default);
- caches licensed voice-clone prompts so reference features are not rebuilt on
  every utterance;
- exposes an OpenAI-compatible streaming HTTP endpoint that the existing
  `Speaches` Pipecat adapter can call **today**, without changing the live
  provider registry;
- exposes the long-term persistent WebSocket protocol at `/v1/tts/ws` with
  `start`, `text`, binary PCM, `cancel_turn`, and `end`;
- outputs raw signed 16-bit PCM and can render 8/16/24/48 kHz on the WebSocket
  path;
- includes auth, concurrency limiting, health, a voice catalog, and minimal
  Prometheus metrics;
- includes a `stub` backend so protocol/API tests run without a GPU or model
  weights.

## Important limitation of this first slice

The public `qwen-tts==0.1.1` Python wrapper returns a complete waveform. Its own
source documents that `non_streaming_mode=False` only simulates streaming text
input and does **not** enable true streaming generation. Therefore v0.1 has a
streaming network contract but the Qwen backend does not yet achieve our TTFA
objective. Cancellation drops unsent audio immediately, but cannot stop the
underlying Qwen GPU thread yet.

That is deliberate. We first lock the service boundary, voice provenance,
telephony rendering, metrics, and runtime integration. The next engine task is
lower-level token/decoder streaming and a cancellable generation loop behind the
same contract.

## Run a no-GPU smoke test

```bash
cd services/hopvoice
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
HOPVOICE_BACKEND=stub HOPVOICE_PRELOAD=0 \
  uvicorn hopvoice.app:app --host 0.0.0.0 --port 8787
```

Set `HOPVOICE_API_KEY=dev-secret` if you want auth enabled in local testing.

## GPU/Qwen run

Build and run with NVIDIA Container Toolkit:

```bash
docker build -t hopvoice:0.1 services/hopvoice

docker run --rm --gpus all -p 8787:8787 \
  -e HOPVOICE_API_KEY='replace-me' \
  -e HOPVOICE_PROFILE=native-fast \
  -e HOPVOICE_MODEL_ID=Qwen/Qwen3-TTS-12Hz-0.6B-Base \
  -e HOPVOICE_VOICES_FILE=/voices/voices.json \
  -v /opt/hopvoice/voices:/voices:ro \
  -v /opt/hopvoice/hf-cache:/home/hopvoice/.cache/huggingface \
  hopvoice:0.1
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

Before the native Pipecat WebSocket adapter lands, the existing `Speaches` TTS
provider can reach Hopvoice through `/v1/audio/speech`:

- provider: `speaches`
- base URL: `http://<hopvoice-host>:8787/v1`
- model: `native-fast`
- voice: your Hopvoice `voice_id`
- API key: `HOPVOICE_API_KEY` (or `none` only on an isolated network with auth disabled)

This path is for first audio/quality benchmarks. The persistent WebSocket path is
the production target because it enables context continuity, server-side
cancellation, future acoustic memory, and lower per-turn overhead.

## WebSocket protocol v1

Client start:

```json
{"type":"start","protocol_version":1,"session_id":"call-uuid","model":"native-fast","voice_id":"alex_licensed_v1","output":{"encoding":"pcm_s16le","sample_rate":8000,"channels":1}}
```

The server answers `ready`. A turn is:

```json
{"type":"text","turn_id":"turn-17","text":"Okay, that makes sense."}
```

The server sends `turn_started`, `audio_meta`, binary PCM frames, then
`turn_done`. Interrupt with:

```json
{"type":"cancel_turn","turn_id":"turn-17"}
```

and finish the call with `{"type":"end"}`.

## Next engineering gates

1. Replace complete-waveform Qwen generation with true token/decoder streaming.
2. Make cancellation stop GPU generation, not merely suppress its output.
3. Add the native Pipecat adapter and provider config in the AI Voice runtime.
4. Add Delivery Planner controls (speech act, emotion, pace, energy, emphasis).
5. Add call-session acoustic continuity.
6. Benchmark native 24 kHz -> G.711 8 kHz vs direct low-rate generation.
7. Gate production on blind human-vs-AI phone tests plus TTFA/cancel-tail metrics.
