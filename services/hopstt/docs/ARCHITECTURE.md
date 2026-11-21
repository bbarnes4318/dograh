# HopSTT architecture

```
                         ┌────────────────────────── Hetzner prod host (Ashburn) ──────────────────────────┐
 PSTN ─► FreeSWITCH ─► Asterisk/ARI ─► Dograh api (Pipecat)                                               │
                         │   transport.input (PCM16 mono 8 kHz)                                            │
                         │      └─► HopSTTService (api/services/pipecat/hopstt_stt.py)                     │
                         │            • 1 WebSocket per call, generation-tagged                            │
                         │            • ring buffer of uncommitted audio, reconnect + failover             │
                         │            • Interim/TranscriptionFrame (same semantics as Cartesia/Dograh)     │
                         │                                                                                  │
                         │  Hoppwhistle api ── call-transcription.ts ── engine "hopstt" (HTTP multipart)    │
                         │     on-demand / grading / Recording Analyzer / recruiting / operator scripts    │
                         │     (stereo split + labels + Postgres transcript cache unchanged)               │
                         └─────────────┬─────────────────────────────────────────┬────────────────────────┘
                                       │ private network, Bearer key            │
                              wss /v1/stt/stream                         POST /v1/transcriptions, /v1/jobs
                                       │                                         │
              ┌────────────────────────▼──────────┐          ┌───────────────────▼────────────────────┐
              │ hopstt-live  (HOPSTT_ROLE=live)   │   ...N   │ hopstt-batch (HOPSTT_ROLE=batch)       │ ...M
              │  admission (MAX_STREAMS) /readyz  │          │  bounded priority queue (normal > low) │
              │  per-session: G.711 decode →      │          │  ffmpeg decode → per-channel float32   │
              │   stateful 8→16 kHz resampler →   │          │  jobs: retry, cancel, TTL              │
              │   bounded audio queue (backpress.)│          │  engines: parakeet-tdt-0.6b-v3 (NeMo)  │
              │  GPU scheduler thread: batched    │          │           faster-whisper turbo (CPU ok)│
              │   cache-aware step() over sessions│          │           Qwen3-ASR-1.7B               │
              │  engines: Nemotron 3.5 streaming  │          └────────────────────────────────────────┘
              │           Nemotron-en streaming   │
              │           Voxtral (vLLM sidecar)  │      Prometheus scrapes /metrics on both roles
              └───────────────────────────────────┘
```

## Principles
- **One contract, many engines.** `PROTOCOL.md` is fixed; engines are adapters chosen by
  model id (`HOPSTT_ALLOWED_MODELS`). Swapping Nemotron for Voxtral is a server config change.
- **Live never waits for batch.** Separate roles/containers (recommended: separate GPUs or
  batch on CPU). Single-GPU mode (`role=both`) enforces a priority gate and a batch
  concurrency cap, and refuses batch above 80 % live utilisation.
- **Genuine streaming.** Cache-aware Conformer: each chunk advances cached encoder state; no
  re-decode of a growing buffer. Many sessions are advanced in one batched forward pass.
- **Caller safety first.** The Dograh client never pushes a non-fatal `ErrorFrame` (which would
  hang up); it reconnects within a deadline, replays only uncommitted audio, drops stale
  generations and duplicate finals. Only fatal/unrecoverable states use the existing
  graceful end-call path.
- **Zero paid STT by default.** Paid fallback exists in both apps only when explicitly enabled,
  is budget-capped per day, logged, and metered.
- **Not on the telephony box.** Inference runs on dedicated hosts reached over a private
  network; the production Hetzner host keeps only the clients.

## Where things live
| Piece | Path |
|---|---|
| Service | `dograh/services/hopstt/hopstt/` |
| Protocol | `dograh/services/hopstt/PROTOCOL.md` |
| Benchmark + capacity tools | `dograh/services/hopstt/bench/` |
| Dograh client/provider | `dograh/api/services/pipecat/hopstt_stt.py`, `api/services/configuration/registry.py` |
| Hoppwhistle client | `hoppwhistle/apps/api/src/services/call-transcription.ts` (+ helper module) |
| Ops docs | `dograh/services/hopstt/docs/` |
