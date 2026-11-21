# HopSTT wire protocol (v1)

This is the contract between the HopSTT inference service and its clients
(Dograh `HopSTTService`, Hoppwhistle `call-transcription.ts`, the benchmark and
capacity tools). Changing a model never changes this contract: models are chosen
by `model` id, resolved server-side against `HOPSTT_ALLOWED_MODELS`.

All endpoints except `/healthz` require `Authorization: Bearer <HOPSTT_API_KEY>`
(constant-time compare). WebSocket clients that cannot set headers may send
`?token=` — disabled unless `HOPSTT_ALLOW_QUERY_TOKEN=true`.

## 1. Operational endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | no | Liveness. `200 {"status":"ok"}` while the process serves HTTP. Never touches the GPU. |
| GET | `/readyz` | no | Readiness. `200` only when every configured engine is loaded **and warmed**, the worker is not draining, and capacity remains. Else `503` with `{"ready":false,"reason":...}`. |
| GET | `/v1/capacity` | yes | `{"worker_id","role":"live|batch|both","draining":bool,"live":{"max_streams","active_streams","available"},"batch":{"max_concurrency","running","queued","queue_limit"},"models":[{"id","kind":"streaming|batch","languages":[...],"chunk_ms":[...],"loaded":bool,"warm":bool}]}`. Used for load-aware routing. |
| GET | `/v1/models` | yes | Allowed models and their capabilities (same `models` list). |
| GET | `/metrics` | no (bind privately) | Prometheus text. Labels are bounded: `model`, `role`, `kind`, `reason`, `language` (`en|es|auto|other`). Never call/tenant ids. |

## 2. Live streaming — `GET /v1/stt/stream` (WebSocket)

Alias: `/api/v1/stt/stream` (same handler; keeps Dograh's `DograhSTTService`
message shape usable).

### 2.1 Client → server

First message MUST be a JSON `config` within `HOPSTT_CONFIG_TIMEOUT_S` (default 5 s):

```json
{
  "type": "config",
  "model": "nemotron-3.5-streaming",      // id from /v1/models; omitted -> server default
  "language": "en",                        // "en" | "es" | "auto" (only if the model supports it)
  "sample_rate": 8000,                     // 8000 | 16000 (others rejected)
  "encoding": "pcm_s16le",                 // "pcm_s16le" | "mulaw" | "alaw"
  "chunk_ms": 320,                         // must be in the model's chunk_ms list; omitted -> model default
  "interim_results": true,
  "vad_events": false,                     // emit speech_started / speech_ended
  "endpointing": {"mode": "client", "silence_ms": 500},  // "client" = utterance ends only on finalize;
                                                          // "server" = also end after silence_ms of non-speech
  "session_id": "opaque-call-scoped-id",   // stable across reconnects of the same call (no PII)
  "generation": 0,                         // incremented by the client on every reconnect
  "stream_offset_ms": 0,                   // audio position of the first byte sent on this connection
  "correlation_id": "opaque",              // for logs only; never a phone number
  "keyterms": []                           // accepted; ignored by engines that cannot bias
}
```

Then:
- **Binary frames**: raw audio in the configured encoding/rate, mono, any size
  that is a whole number of samples, in order. Ordering is the WebSocket's.
- `{"type":"finalize"}`: end the current utterance now (client VAD stop). Server
  flushes the model's right-context with silence padding and emits the final.
- `{"type":"keepalive"}`: resets the idle timer. No reply.
- `{"type":"end_of_stream"}`: finalize, send `closed`, close 1000.
- `{"type":"cancel"}`: drop all pending audio, no final, send `closed`, close 1000.

### 2.2 Server → client

```json
{"type":"ready","session_id":"...","generation":0,"model":"nemotron-3.5-streaming",
 "engine":"nemo-cache-aware","chunk_ms":320,"sample_rate":8000,"worker_id":"..."}

{"type":"transcript","text":"my date of birth is","is_final":false,"from_finalize":false,
 "confidence":0.0,"language":"en","seq":17,"utterance_id":3,"generation":0,
 "start_ms":10240,"end_ms":12160}

{"type":"transcript","text":"My date of birth is March 3rd, 1951.","is_final":true,"from_finalize":true,
 "confidence":0.93,"language":"en","seq":18,"utterance_id":3,"generation":0,
 "start_ms":10240,"end_ms":12800}

{"type":"speech_started","seq":19,"generation":0,"at_ms":15020}
{"type":"speech_ended","seq":20,"generation":0,"at_ms":16100}

{"type":"error","code":"capacity","message":"...","retryable":true,"fatal":false}
{"type":"closed","reason":"end_of_stream"}
```

Semantics:
- `seq` is strictly increasing per connection, starting at 1. Clients drop any
  message whose `generation` is not the current one (stale after reconnect).
- **Interim** `text` is the full current hypothesis for the open utterance (not a
  delta). Interims are emitted only when the text changes.
- **Final** is emitted exactly once per `utterance_id`. After a final, the next
  utterance gets `utterance_id + 1`. A finalize with no speech yields no final.
- `start_ms`/`end_ms` are positions in the **call's** audio timeline:
  `stream_offset_ms` + audio received on this connection. `end_ms` of a final is
  the commit point: audio before it never needs to be resent.
- `confidence` is 0..1 when the engine provides one, else omitted.
- `language` is the requested language, or the detected one for `auto`.

### 2.3 Errors and close codes

| `code` | retryable | fatal | close | Meaning |
|---|---|---|---|---|
| `auth_failed` | no | yes | 1008 | Bad/missing key. |
| `invalid_config` | no | yes | 1008 | Unknown model, unsupported language/rate/encoding/chunk. |
| `config_timeout` | yes | no | 1008 | No config in time. |
| `capacity` | yes | no | 1013 | Worker full. Try another instance. |
| `draining` | yes | no | 1012 | Worker shutting down. Reconnect elsewhere. |
| `overloaded` | yes | no | 1013 | Session fell more than `HOPSTT_MAX_LAG_MS` behind real time. |
| `idle_timeout` | yes | no | 1000 | No audio/keepalive for `HOPSTT_IDLE_TIMEOUT_S`. |
| `model_error` | yes | no | 1011 | Inference failed for this session. |
| `internal` | yes | no | 1011 | Unexpected server error. |

### 2.4 Flow control

- The per-session audio queue is bounded to `HOPSTT_MAX_QUEUE_MS` (default 2000 ms
  of audio). When full, the server stops reading the socket (TCP backpressure).
- If the session's processed position falls more than `HOPSTT_MAX_LAG_MS` (default
  3000) behind received audio, the server sends `overloaded` and closes.
- Frames larger than `HOPSTT_MAX_FRAME_BYTES` (default 64 KiB) → `invalid_config`.
- Admission: if `active_streams >= HOPSTT_MAX_STREAMS`, the server accepts the
  upgrade, sends `capacity`, and closes 1013, without loading any state.

### 2.5 Client reconnect contract (what Dograh implements)

On a retryable error or dropped socket, the client:
1. increments `generation`, opens a new connection (same or another instance) with
   bounded exponential backoff + jitter, within a recovery deadline;
2. sends `config` with the same `session_id`, the new `generation`, and
   `stream_offset_ms` = the last committed `end_ms`;
3. resends only the **uncommitted** audio (since the last final's `end_ms`, capped
   by a bounded ring buffer), then resumes live audio;
4. ignores every message from older generations, and drops any final whose
   `(start_ms, end_ms)` lies entirely at or before the committed point.
Fatal errors, or exceeding the recovery deadline, are surfaced to the pipeline
as a fatal error (existing graceful hang-up path).

## 3. Recorded audio — batch

### 3.1 `POST /v1/transcriptions` (synchronous, short recordings)

`multipart/form-data`:

| Field | Default | Notes |
|---|---|---|
| `file` | required | WAV (PCM16/float/μ-law/A-law, any channel count), MP3, FLAC, OGG/Opus, WebM, MP4/M4A. Max `HOPSTT_MAX_UPLOAD_MB`. Decoded with ffmpeg server-side. |
| `model` | server default batch model | id from `/v1/models` |
| `language` | `en` | `en` \| `es` \| `auto` (only if the model supports it) |
| `channels` | `split` | `split`: each channel transcribed independently (stereo call legs); `mix`: down-mix to mono |
| `word_timestamps` | `true` | ignored when the model cannot produce them reliably (`words` omitted) |
| `priority` | `normal` | `normal` \| `low` (historical backfill) |

Rejected with `413` if the decoded duration exceeds `HOPSTT_SYNC_MAX_SECONDS`
(default 900): use a job.

Response `200`:

```json
{
  "model": "parakeet-tdt-0.6b-v3",
  "engine": "nemo-offline",
  "duration_sec": 143.8,
  "sample_rate": 8000,
  "channels": [
    {
      "channel": 0,
      "language": "en",
      "text": "...",
      "segments": [
        {"start": 0.42, "end": 3.10, "text": "Hello, this is Alex.",
         "words": [{"word": "Hello,", "start": 0.42, "end": 0.71, "confidence": 0.98}]}
      ]
    }
  ],
  "timings": {"decode_ms": 12, "queue_ms": 0, "inference_ms": 2210}
}
```

Speaker identity is **not** inferred: the client maps channel index → role from
what it knows (e.g. FreeSWITCH inbound: ch0 caller, ch1 agent).

### 3.2 Jobs (long recordings, backlog)

- `POST /v1/jobs` — same form as above → `202 {"job_id","status":"queued"}`.
  `429` when the batch queue is at `HOPSTT_BATCH_QUEUE_LIMIT`.
- `GET /v1/jobs/{id}` → `{"job_id","status":"queued|running|succeeded|failed|cancelled","attempts","error"?,"result"?}`.
  Results are kept in memory for `HOPSTT_JOB_TTL_S` (default 3600), then deleted.
- `DELETE /v1/jobs/{id}` → cancel (`200`, idempotent).
- Transient engine failures are retried up to `HOPSTT_JOB_MAX_ATTEMPTS` (default 2)
  with backoff. `priority=low` jobs run only when no `normal` job is waiting.

Batch audio and transcripts are never written to disk except a temp file during
decode, deleted in `finally`.

## 4. Workload isolation

`HOPSTT_ROLE`:
- `live` — only `/v1/stt/stream`; batch endpoints return `404`.
- `batch` — only batch endpoints; the stream endpoint returns `capacity`.
- `both` — one process, one GPU: live inference always runs first; batch runs
  at most `HOPSTT_BATCH_MAX_CONCURRENCY` (default 1) and pauses between
  chunks whenever live work is pending (priority gate), and is refused
  entirely while `active_streams / max_streams >= HOPSTT_BATCH_PAUSE_AT_LIVE_UTIL`
  (default 0.8).

Recommended production: separate `live` and `batch` containers (separate GPUs,
or batch on CPU with faster-whisper/parakeet CPU), each scaled independently.

## 5. Graceful shutdown

On SIGTERM: `/readyz` → 503 immediately, new streams get `draining`, running
streams continue until they end or `HOPSTT_DRAIN_TIMEOUT_S` (default 600, same
as FreeSWITCH deploy wait); then remaining sessions get `draining` (retryable,
so Dograh reconnects to another instance), batch jobs are failed as retryable.
