# HopSTT — Hopwhistle self-hosted speech-to-text

HopSTT is a first-party GPU inference service that replaces chargeable STT
for Dograh live calls (WebSocket streaming) and Hoppwhistle recorded calls
(HTTP batch). The wire contract is **[PROTOCOL.md](PROTOCOL.md)**; changing a
model never changes it. Background, licences and model facts:
[INVESTIGATION.md](INVESTIGATION.md), [LICENSES.md](LICENSES.md), `docs/`.

```
Dograh HopSTTService ─ wss /v1/stt/stream ─┐        ┌─ POST /v1/transcriptions, /v1/jobs ─ Hoppwhistle
                                            ▼        ▼
 hopstt-live (role=live)                         hopstt-batch (role=batch)
  auth → admission (MAX_STREAMS) → config         auth → body size limit (streamed)
  G.711/PCM16 decode (odd-byte carry)             ffmpeg (no shell, timeout, temp file
  stateful 8→16 kHz polyphase resampler            deleted in finally) → float32 per channel
  energy VAD (vad_events / server endpointing)    bounded queue: normal before low
  chunker → bounded per-session queue             N workers: retry, cancel, TTL job store
    (stop reading the socket when full)           engines: parakeet-tdt-v3 / faster-whisper /
  scheduler thread (one per GPU):                          Qwen3-ASR
    1 item per session per tick, grouped by
    (engine, chunk_ms) → ONE batched step()
  engines: Nemotron 3.5 / Nemotron-en (NeMo cache-aware), Voxtral (vLLM proxy)
```

## Package layout

| Path | What |
|---|---|
| `hopstt/config.py` | every `HOPSTT_*` setting; refuses to start without a ≥16-char key unless `HOPSTT_DEV_INSECURE=true` |
| `hopstt/models.py` | model catalogue: id → engine, pinned HF commit, sha256, languages, chunk sizes |
| `hopstt/audio.py` | G.711 tables, PCM16, `FrameDecoder`, `StreamingResampler`, `Chunker`, `Timeline`, ffmpeg decode |
| `hopstt/session.py` | per-connection state shared by the socket handler and the scheduler; energy VAD |
| `hopstt/scheduler.py` | `LiveScheduler` (batched streaming inference thread), `BatchPool` (jobs) |
| `hopstt/server.py` | FastAPI app, WebSocket handler, batch endpoints, drain-on-SIGTERM entrypoint |
| `hopstt/metrics.py` | Prometheus metrics (bounded labels only) |
| `hopstt/engines/` | `base.py` interfaces; `stub`, `nemo_cache_aware`, `vllm_realtime`, `nemo_offline`, `faster_whisper`, `qwen3_asr` |
| `hopstt/weights.py`, `hopstt/tools/fetch_models.py` | pinned weight resolution, download + verification |
| `hopstt/tools/smoke.py` | streams a WAV like a live call and reports finalize→final latency |

### Engines

| Model id | Kind | Engine | Languages | chunk_ms | Notes |
|---|---|---|---|---|---|
| `nemotron-3.5-streaming` | streaming | `nemo-cache-aware` | en, es, auto | 80/160/320/560/1120 (default 320) | att_context `[56, {0,1,3,6,13}]`; per-session language prompt; `<xx-XX>` tag stripped and reported as `language` in auto mode |
| `nemotron-en-streaming` | streaming | `nemo-cache-aware` | en | 80/160/560/1120 | att_context `[70, {0,1,6,13}]`; NVIDIA Open Model License |
| `voxtral-realtime` | streaming | `vllm-realtime` | en, es, auto | 80…480 send granularity | proxies to a separate `vllm serve` `/v1/realtime`; delay set on vLLM (480 ms) and mirrored in `HOPSTT_VOXTRAL_DELAY_MS` |
| `parakeet-tdt-0.6b-v3` | batch | `nemo-offline` | en, es, auto | – | word timestamps; local attention for > 20 min |
| `whisper-large-v3-turbo` | batch | `faster-whisper` | en, es, auto | – | `vad_filter=True`, `condition_on_previous_text=False` |
| `whisper-base` | batch | `faster-whisper` | en, es, auto | – | CPU dev/smoke only |
| `qwen3-asr-1.7b` | batch | `qwen3-asr` | en, es, auto | – | no word timestamps (aligner not wired); own image |
| `stub-streaming`, `stub-batch` | both | `stub` | en, es, auto | 80…1120 | deterministic, no deps; used by all no-GPU tests and the CPU dev compose |

**How the streaming engine works.** `nemo_cache_aware` reproduces NeMo's
`speech_to_text_cache_aware_streaming_infer.py` (`CacheAwareStreamingAudioBuffer`
+ `conformer_stream_step`) for live audio: each session computes only its new
mel frames from a short raw-audio tail aligned to the 10 ms hop (verified
identical to the offline spectrogram: max abs diff 0.0 on real speech),
prepends the 9-frame pre-encode cache, and keeps `cache_last_channel /
cache_last_time / cache_last_channel_len` plus its RNNT partial hypothesis.
Ready sessions with the same `chunk_ms` are concatenated on the batch axis
for one encoder + prompt + greedy-batch decoder call per tick, then split.
`finalize` pushes the tail plus enough silence to complete the chunk, reads
the final, resets the decoder hypothesis (encoder caches continue). Nothing
is ever re-transcribed.

### Live protocol behaviour worth knowing

- `seq` is on **every** server message (including `ready`, `error`, `closed`)
  and strictly increases from 1; `generation` is echoed on every message.
- Interims are sent only when the text changes; a final is sent once per
  `utterance_id`; a finalize with no speech sends nothing.
- `end_ms` of a final is the commit point = `stream_offset_ms` + all audio
  received before the finalize. Interim `end_ms` = processed position.
  `start_ms` is approximate (two chunks before the first text, never before
  the previous commit) — engines here do not expose reliable token times.
- Reading pauses at `HOPSTT_MAX_QUEUE_MS` and resumes at half of it. Lag (for
  `overloaded`) is the client's estimated real-time position minus processed
  audio: once we stop reading, audio sits in kernel TCP buffers we cannot
  count, so the position is extrapolated from the wall clock while the
  session is behind, and re-anchored whenever it is caught up (so
  keepalive-only pauses are not lag). A client sending faster than real time
  is never flagged just for that.
- One failed batched step fails every session in that step with a retryable
  `model_error` (clients reconnect and resend uncommitted audio).
- `endpointing.mode = "server"` uses the energy VAD (`HOPSTT_VAD_THRESHOLD_DBFS`,
  `silence_ms`); finals then have `from_finalize: false`. Nemotron has no
  EOU token, so there is no model-based endpointing yet.
- `keyterms` are accepted and ignored by all current engines.

## Roles and deployment

`HOPSTT_ROLE=live` serves only the stream (batch endpoints 404),
`batch` only batch (stream gets `capacity`), `both` one process on one GPU:
live first, batch at most `HOPSTT_BATCH_MAX_CONCURRENCY`, paused between
internal chunks while live work is pending, refused (`503 live_priority`) at
`HOPSTT_BATCH_PAUSE_AT_LIVE_UTIL`.

### GPU (recommended: split live / batch)

```bash
cd services/hopstt
cp env.example .env     # HOPSTT_API_KEY, HOPSTT_BIND=<private-ip>, HOPSTT_LIVE_GPU / HOPSTT_BATCH_GPU
docker compose -f docker-compose.gpu.yaml up -d --build
curl http://<private-ip>:8790/readyz        # live  {"ready":true}
curl http://<private-ip>:8791/readyz        # batch
curl -H "Authorization: Bearer $HOPSTT_API_KEY" http://<private-ip>:8790/v1/capacity
python -m hopstt.tools.smoke --url ws://<private-ip>:8790/v1/stt/stream --key $HOPSTT_API_KEY \
    --wav synthetic_8k.wav --realtime           # finalize→final p50 should be well under 300 ms on GPU
```

The compose healthchecks use `/readyz`, which is also 503 while a worker is
full (`no_capacity`) or draining: "unhealthy" there means "do not route new
work here", not "restart me" (no autoheal). Liveness is `/healthz`.

`hopstt-fetch` runs first and populates the `hopstt-models` volume; live and
batch start only after it exits 0, and run with `HF_HUB_OFFLINE=1`. Each
service can be pinned to its own GPU. A one-GPU host can run both on GPU 0
(no cross-process priority) or a single `role=both` container.

### CPU (development, CPU batch)

```bash
HOPSTT_API_KEY=dev-key-0123456789ab docker compose -f docker-compose.cpu.yaml up --build
```

Stub streaming (protocol only) + faster-whisper `whisper-base` int8 batch.
For production CPU batch build `Dockerfile.cpu` with `HOPSTT_EXTRAS=nemo,whisper,fetch`
and run `role=batch` with parakeet or whisper turbo.

### Without Docker

```bash
pip install -e '.[nemo,whisper,fetch]'      # CPU: install torch from the CPU index first
HOPSTT_DEV_INSECURE=true HOPSTT_MODEL_CACHE=./models python -m hopstt.tools.fetch_models
HOPSTT_API_KEY=... HOPSTT_DEVICE=cuda:0 HOPSTT_MODEL_CACHE=./models python -m hopstt
```

Always start with `python -m hopstt` (not bare `uvicorn`): the first SIGTERM
drains (`/readyz` 503, new streams `draining`, running streams continue up to
`HOPSTT_DRAIN_TIMEOUT_S`, then get `draining`), a second exits immediately.
Compose sets `stop_grace_period: 11m` accordingly.

## Model fetch, cache, pinning

- Revisions are pinned in `hopstt/models.py` (`main` on 2026-11-21) with the
  sha256 Hugging Face publishes for each weight file.
- `python -m hopstt.tools.fetch_models [--models id ...]` downloads exactly
  those revisions into `HOPSTT_MODEL_CACHE` and verifies files and sha256
  (exit 1 on mismatch). `--verify-only` checks an existing cache.
- At startup weights are resolved with `local_files_only` unless
  `HOPSTT_ALLOW_DOWNLOAD=true`. `HOPSTT_MODEL_PATHS=id=/path` points at a
  local file (air-gapped hosts).
- Override a revision with `HOPSTT_MODEL_REVISIONS=id=<commit>`; re-run fetch.

## Upgrade / rollback

A deployment is identified by **image tag + pinned revisions**. To upgrade:
build a new tag (`HOPSTT_IMAGE=hopstt:0.2.0`), run `hopstt-fetch` for any new
revision, then `docker compose up -d` one service at a time; the old
container drains for up to 10 min while Dograh reconnects new streams to
healthy workers. To roll back, set the previous `HOPSTT_IMAGE` and
`HOPSTT_MODEL_REVISIONS` (the old weights stay in the cache volume) and
`up -d` again. Never restart a live worker during business hours without
another live worker in rotation.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `HOPSTT_API_KEY` | – (required) | Bearer key, ≥16 chars, constant-time compared |
| `HOPSTT_DEV_INSECURE` | false | allow running without a key (dev only) |
| `HOPSTT_ALLOW_QUERY_TOKEN` | false | accept `?token=` on the WebSocket |
| `HOPSTT_ROLE` | both | `live` \| `batch` \| `both` |
| `HOPSTT_WORKER_ID` | hostname | reported in `ready` and `/v1/capacity` |
| `HOPSTT_HOST` / `HOPSTT_PORT` | 0.0.0.0 / 8790 | listen address inside the container |
| `HOPSTT_ALLOWED_MODELS` | nemotron-3.5-streaming,parakeet-tdt-0.6b-v3 | requests for others → `invalid_config` / 400 |
| `HOPSTT_DEFAULT_STREAMING_MODEL` / `HOPSTT_DEFAULT_BATCH_MODEL` | nemotron-3.5-streaming / parakeet-tdt-0.6b-v3 | used when `model` is omitted |
| `HOPSTT_DEVICE` | cuda:0 | `cuda:N` or `cpu` |
| `HOPSTT_MODEL_CACHE` | /models | HF cache directory (volume) |
| `HOPSTT_MODEL_REVISIONS` | – | `id=<commit>,…` overrides |
| `HOPSTT_MODEL_PATHS` | – | `id=/path,…` local weights |
| `HOPSTT_ALLOW_DOWNLOAD` | false | download missing pinned weights at startup |
| `HOPSTT_WARMUP` | true | warm every engine (all chunk sizes) before ready |
| `HOPSTT_MAX_STREAMS` | 64 | admission limit (`capacity` beyond it) |
| `HOPSTT_MAX_BATCH_SIZE` | 64 | sessions per streaming forward pass |
| `HOPSTT_CONFIG_TIMEOUT_S` | 5 | first message deadline |
| `HOPSTT_IDLE_TIMEOUT_S` | 30 | no audio/keepalive → `idle_timeout` |
| `HOPSTT_MAX_QUEUE_MS` | 2000 | per-session audio queue bound (backpressure) |
| `HOPSTT_MAX_LAG_MS` | 3000 | → `overloaded` |
| `HOPSTT_MAX_FRAME_BYTES` | 65536 | larger frame → `invalid_config` |
| `HOPSTT_VAD_THRESHOLD_DBFS` / `HOPSTT_VAD_MIN_SPEECH_MS` | -45 / 60 | energy VAD for `vad_events` and server endpointing |
| `HOPSTT_MAX_UPLOAD_MB` | 200 | enforced while the body streams (413) |
| `HOPSTT_SYNC_MAX_SECONDS` | 900 | longer → 413, use `/v1/jobs` |
| `HOPSTT_BATCH_MAX_CONCURRENCY` | 1 | batch workers |
| `HOPSTT_BATCH_QUEUE_LIMIT` | 100 | queued jobs before 429 |
| `HOPSTT_BATCH_PAUSE_AT_LIVE_UTIL` | 0.8 | role=both: refuse batch above this live utilisation |
| `HOPSTT_JOB_TTL_S` / `HOPSTT_JOB_MAX_ATTEMPTS` / `HOPSTT_JOB_RETRY_BACKOFF_S` | 3600 / 2 / 2 | job store and retries |
| `HOPSTT_FFMPEG_TIMEOUT_S` | 300 | ffprobe/ffmpeg timeout |
| `HOPSTT_WHISPER_COMPUTE_TYPE` / `HOPSTT_WHISPER_BEAM_SIZE` | auto / 5 | faster-whisper |
| `HOPSTT_VLLM_URL` / `HOPSTT_VLLM_MODEL` / `HOPSTT_VOXTRAL_DELAY_MS` | ws://127.0.0.1:8000/v1/realtime / Voxtral repo / 480 | Voxtral via vLLM |
| `HOPSTT_DRAIN_TIMEOUT_S` | 600 | SIGTERM drain window |
| `HOPSTT_STUB_STEP_LATENCY_MS` / `HOPSTT_STUB_BATCH_LATENCY_MS` | 0 | stub: simulated GPU time |
| `HOPSTT_LOG_LEVEL` | info | |

Logs never contain transcript text or audio; session ids are logged as a
truncated sha256; metrics labels are limited to model, role, kind, reason,
status, priority and language (`en|es|auto|other`).

## Metrics

`hopstt_active_streams`, `hopstt_live_capacity_available`, `hopstt_ready`,
`hopstt_draining`, `hopstt_stream_opens_total`, `hopstt_stream_reconnect_opens_total`
(generation > 0), `hopstt_stream_closes_total{reason}`, `hopstt_stream_rejects_total`,
`hopstt_stream_errors_total{code}`, `hopstt_partial_latency_seconds`,
`hopstt_final_latency_seconds` (finalize → final), `hopstt_inference_step_seconds`,
`hopstt_inference_batch_size`, `hopstt_inference_step_rtf`, `hopstt_live_queue_audio_ms`,
`hopstt_live_lag_ms`, `hopstt_audio_seconds_total{kind,model}` (cost per minute =
GPU-hour cost / (rate × 60)), `hopstt_batch_jobs_total{kind,status}`,
`hopstt_batch_queue_depth{priority}`, `hopstt_batch_running`,
`hopstt_batch_inference_seconds`, `hopstt_batch_queue_seconds`,
`hopstt_batch_retries_total`, `hopstt_cuda_memory_*_bytes`,
`hopstt_gpu_utilization_percent` / `hopstt_gpu_memory_used_bytes` (with `nvidia-ml-py`).

## Tests: what is real and what is mocked

```bash
pip install -e '.[test]' && python -m pytest -q      # base deps only, stub engines, ffmpeg optional
```

- **Stub-only (CI):** the whole protocol — auth, config validation, ordering,
  interim dedupe, final-once, silence, isolation across 24 concurrent streams,
  backpressure, lag→`overloaded`, admission, timeouts, cancel, end_of_stream,
  forced-disconnect cleanup, drain, roles, batch sync/jobs/cancel/retry/
  priority/TTL/size limits, metrics label bounds. G.711 is checked against
  ffmpeg, the resampler against `scipy.signal.resample_poly`.
- **Mocked:** `vllm_realtime` against a fake `/v1/realtime` server
  (protocol only; Voxtral itself needs a ≥16 GB GPU).
- **Real (opt-in):** `HOPSTT_REAL_MODEL_TESTS=1 HOPSTT_TEST_WAV=... pytest tests/test_real_model.py`
  (`real_model` / `gpu` markers). Nemotron 3.5 streaming, parakeet-tdt-v3 and
  whisper-base were run for real on CPU in development (see the PR notes for
  numbers); none of this repo's numbers are GPU numbers.
- **Not exercised here:** CUDA, `nemotron-en-streaming`, Qwen3-ASR,
  whisper-large-v3-turbo, Voxtral with real weights, the Docker images.
