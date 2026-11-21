# Self-hosted STT — Phase 1 dependency map and Phase 2 evaluation plan

Date: 2026-11-21. Scope: replace chargeable Cartesia STT in Dograh (live calls) and
Hoppwhistle (recorded calls) with a self-hosted service. Code references are
`file:line` at the commits current on that date (dograh `e4b5e0f`, hoppwhistle
`7e59888`, pipecat fork `38bf23d`).

---

## 1. Dograh (live calls)

### 1.1 Audio path to STT
- Production telephony reaches Dograh through Asterisk/ARI (FreeSWITCH hands off to
  Asterisk; `api/services/telephony/inbound_admission.py:3-12`). `AsteriskFrameSerializer`
  decodes PCMU to **PCM16 mono at 8 kHz** (`ari/transport.py:45-56`, `ari/__init__.py:185`).
- `create_audio_config()` (`api/services/pipecat/audio_config.py:73-105`) sets every rate
  to the provider's `transport_sample_rate`: 8 kHz for Twilio/Telnyx/Plivo/Vobiz/Cloudonix/ARI,
  16 kHz for Vonage/WebRTC.
- Every STT branch gets `sample_rate=audio_config.transport_in_sample_rate`. **Nothing
  upsamples for STT** → the self-hosted server must accept 8 kHz and resample to 16 kHz itself.
- Pipeline order (`pipeline_builder.py:77-135`): `transport.input → dtmf → STT →
  MachineAnswerGuard → voicemail detector → muted-speech buffer → user aggregator → LLM …`.

### 1.2 Provider selection
- `create_stt_service()` (`service_factory.py:251-550`), if/elif on `user_config.stt.provider`,
  14 providers. Called from `run_pipeline.py:727-732` only for non-realtime workflows.
- Cartesia branch (`:332-348`): `ink-2` → `CartesiaTurnsSTTService` (STT owns turns,
  `stt_uses_external_turns()` `:182-189`); otherwise `CartesiaSTTService(model, language or "en")`,
  a websocket STT that sends `finalize` on `VADUserStoppedSpeakingFrame`.
- Dograh branch (`:349-385`) uses `DograhSTTService` — a WebSocket protocol we can mirror:
  `GET {base}/api/v1/stt/stream`, Bearer auth, JSON `config` message, binary PCM frames,
  `keepalive`, `finalize`, `end_of_stream`; server replies `{text,is_final,from_finalize,confidence,language}`
  plus `speech_started/speech_ended`. It is tied to the global `MPS_API_URL`, so reuse the
  protocol, not the branch.
- HuggingFace (`:433-448`) and Speaches (`:421-432`) are segmented HTTP (post-VAD), not streaming.
- Best in-repo template for a first-party self-hosted service: **Hopvoice TTS**
  (`api/services/pipecat/hopvoice_tts.py`, registry `registry.py:1753-1803`, gate
  `HOPVOICE_ENABLED` `service_factory.py:714-749`, validator `check_validity.py:138-161`,
  GPU sidecar `services/hopvoice/`).
- The fork also ships `NvidiaSTTService` (Riva gRPC, `pipecat/services/nvidia/stt.py:225`), but
  the `nvidia` extra is not installed and it ties us to Riva/NIM.

### 1.3 Interim vs final transcripts
Consumers: `MachineAnswerGuard` (interims → immediate machine-keyword hangup; finals →
classification, `call_hygiene.py:344-407`); turn-start strategies (interims by default);
muted-speech buffer (finals); realtime UI feed (both); `contains_user_speech()` → `user_speech`
tag (finals). **Without interims, barge-in and machine hangup degrade to final-only latency.**

### 1.4 End-of-turn
`run_pipeline.py:965-1014`: Silero VAD `stop_secs=0.2`, VAD frames broadcast upstream to STT
(trigger finalize). Stop strategy: `SpeechTimeoutUserTurnStopStrategy` (0.6 s, waits for
transcript using the STT's `ttfs_p99_latency`; Cartesia 0.81 s, default 1.0 s,
`pipecat/services/stt_latency.py:38-57`), or `LocalSmartTurnAnalyzerV3` (stop 2.0 s), or
external turns. **The new service must report a measured `ttfs_p99_latency`** and must
finalize fast on `finalize` — this is the single biggest latency lever.

### 1.5 Interruptions
Aggregator emits `InterruptionFrame` from VAD/transcription start strategies; STT built with
`should_interrupt=False` when it owns turns. Mute strategies in `run_pipeline.py:277-299`,
muted speech replayed by `muted_speech_buffer.py`.

### 1.6 Configurable models / UI
`STTConfig` discriminated union (`registry.py:2210-2229`); Cartesia: `ink-whisper` (default),
`ink-2`. Base config requires `api_key` — a self-hosted config must make it optional (as
Speaches/Hopvoice do). UI is **schema-driven** (`routes/user.py:64-90`,
`ServiceConfigurationForm.tsx`): a new registry class appears automatically.

### 1.7 How config reaches agents
Org row `MODEL_CONFIGURATION_V2` (JSON) → optional per-workflow
`model_configuration_v2_override` → runs use the pinned definition. Changing the org default
affects every workflow without its own STT override from its next call; workflows with an
override need a JSON data update. **No SQL migration.** Hazard: an org row that fails
validation silently becomes empty (`ai_model_configuration.py:141-154`) — so **keep
`cartesia` in the union**; switch rows, don't delete the provider.

### 1.8 STT errors → transfers / voicemail / qualification
- **Any upstream `ErrorFrame` ends the call** (`pipecat/pipeline/worker.py:1176-1183` →
  `event_handlers.py:201-226`, `PIPELINE_ERROR`, plus campaign circuit-breaker). Websocket
  reconnect paths push ErrorFrames. The new client must reconnect silently and push an
  ErrorFrame only on terminal failure.
- Missing transcripts → no machine/screener detection → transfer guard
  (`pipecat_engine_custom_tools.py:884-911`) can't block a voicemail transfer; idle ladder
  hangs up "silent" callers; qualification tools (LLM-driven) never fire.
- Voicemail detector (LLM over transcripts) reads the live workflow config, not the pinned one
  (`run_pipeline.py:1179-1220`).

### 1.9 STT vs speech-to-speech
Realtime mode (OpenAI/Azure Realtime, Gemini Live/Vertex, Grok, Ultravox — `realtime/*.py`)
sets `stt=None`, and has no voicemail detector or call hygiene. Out of scope for this project.

### 1.10 No pipecat changes required
Subclass `WebsocketSTTService` in `api/services/pipecat/`, add registry class + factory branch
+ validator + tests. `url_security` is a no-op in `DEPLOYMENT_MODE=oss` (production);
in SaaS mode it rejects private IPs/hostnames.

### 1.11 Evals
`evals/stt/` streams audio at 8 kHz/80 ms chunks to raw websocket providers (Deepgram,
Speechmatics, smart-turn). **No WER, no reference transcripts, no Cartesia provider.**

---

## 2. Hoppwhistle (recorded calls)

All live paths go through `apps/api/src/services/call-transcription.ts` inside the API container.

| Claim | Verdict |
|---|---|
| Cartesia prioritized | **Confirmed.** `availableEngines()` `:91-97` = cartesia → deepgram → gemini by key presence; `POST https://api.cartesia.ai/stt`, `ink-whisper`, language fixed by `CARTESIA_STT_LANGUAGE` (default `en`) per process. On-demand uses the default engine only; cross-engine fallback only in call-quality grading (`call-quality/jobs.ts:504-536`). |
| Saved-transcript cache | **Confirmed.** Postgres `transcriptions` (`schema.prisma:1858-1875`), keyed `(callId, recordingId)`, newest wins; `metadata = {engine, model, durationSec, segments}`; in-process dedupe map. |
| Stereo channel split | **Confirmed, pure JS (no ffmpeg).** `splitWavChannels()` `:281-345`; both legs transcribed in parallel, words merged by time (`:365-392`). Labels: INBOUND → ch0 Caller / ch1 Agent; others → "Speaker 1/2". |
| Faster-Whisper worker undeployed | **Confirmed for the deploy path** (`scripts/deploy-production.sh` uses `docker-compose.dev.yml`, which has no transcriber). Statement lives in `call-transcription.ts:12-14`, not the media README. Still defined in the base compose / `app.yaml` / runbooks, and internally broken (spawns missing `main.py`, wrong Redis stream). Treat as dead code. |

Reachable transcription paths in production:
1. `POST /api/v1/calls/:callId/transcription` and `GET …/transcription.txt` (`routes/index.ts:4983-5055`) — user click/download.
2. Bulk zip export — saved rows only.
3. `CallQualityWorker` (gated by `CALL_QUALITY_ENABLED`, default false) — on `recording.ready`, 10-min sweep.
4. **Recording Analyzer auto-sweep every 2 min, on by default, lookback can be all history** (`recording-analysis/auto-calls.ts`) — **the main STT volume.** Uploads/URLs bypass the cache.
5. Recruiting candidate media (`services/recruiting/transcription.ts`) — webm/mp4.
6. Operator scripts (`scripts/agent-call-report.mjs`, `campaign-transcripts.mjs`, `inbound-call-export.mjs`) — **duplicate copies** of the Cartesia HTTP call.
- Dograh-ingested calls reuse Dograh's transcripts (no STT).

Audio: stereo WAV from FreeSWITCH; outbound dialplan records 16 kHz, inbound records at codec
rate (likely 8 kHz). Read from `/recordings` volume, MinIO, or URL.

Integration point: one new engine (`selfhosted`) in `availableEngines`/`transcribeAudio`
returning the existing `TranscriptResult`/`segments` shape; keep the split/merge/cache code.
Recruiting webm/mp4 needs server-side decode (ffmpeg in the STT container).

---

## 3. Phase 2 — candidate facts (from official model pages, verified 2026-11-21)

| Model | License | Commercial | EN | ES | Streaming | Notes |
|---|---|---|---|---|---|---|
| nvidia/nemotron-3.5-asr-streaming-0.6b | **OpenMDW-1.1** (permissive, no use restrictions) | Yes | ✓ | ✓ (es-US, es-ES "transcription-ready") | cache-aware, 80/160/320/560/1120 ms | 16 kHz mono; punct+caps; FLEURS WER en 8.27 / es 4.39 @320 ms; H100: ~240 streams @80 ms, ~2400 @1120 ms (vendor claim); NeMo 26.06 |
| nvidia/nemotron-speech-streaming-en-0.6b | NVIDIA Open Model License (guardrail + revocation clauses) | Yes | ✓ | ✗ | 80/160/560/1120 ms | English-only; NVIDIA recommends it for EN-only |
| mistralai/Voxtral-Mini-4B-Realtime-2602 | Apache-2.0 | Yes | ✓ | ✓ | delay 80–1200 ms (480 recommended) | ~4.4B, ≥16 GB GPU, vLLM; FLEURS en 4.90 / es 3.31 @480 ms; SwitchBoard 11.65 |
| kyutai/stt-1b-en_fr | CC-BY-4.0 | Yes (attribution) | ✓ | **✗** | fixed 0.5 s | 24 kHz input; Rust moshi-server; semantic VAD. **Excluded: no Spanish.** |
| nvidia/parakeet_realtime_eou_120m-v1 | NVIDIA OML | Yes | ✓ | **✗** | 80–160 ms | emits `<EOU>`, **no punctuation/caps**. Only viable as an auxiliary EN end-of-turn signal. |
| nvidia/parakeet-tdt-0.6b-v3 | CC-BY-4.0 | Yes (attribution) | ✓ | ✓ | offline/chunked | word timestamps, long audio; FLEURS en 4.85 / es 3.45 |
| Qwen/Qwen3-ASR-1.7B | Apache-2.0 | Yes | ✓ | ✓ | vLLM only, no timestamps when streaming | timestamps need separate aligner |
| Whisper large-v3-turbo (faster-whisper, `dropbox-dash/faster-whisper-large-v3-turbo`) | MIT | Yes | ✓ | ✓ | no | known silence hallucination risk; needs VAD filter |

None of these numbers are 8 kHz telephone audio. **No model is declared the winner from them.**

### Provisional choice (to be confirmed by our own benchmark)
- **Live:** Nemotron 3.5 streaming at 320 ms chunks (one model for EN+ES, permissive licence,
  highest claimed concurrency). Challenger: Voxtral Realtime @480 ms (higher published accuracy,
  ~7× the parameters, lower concurrency). Fallback for EN-only if 3.5 underperforms:
  nemotron-speech-streaming-en.
- **Recorded:** Parakeet-TDT-0.6b-v3 (EN+ES, word timestamps needed for the stereo
  merge). Challengers: Qwen3-ASR-1.7B, faster-whisper turbo. Also measure Nemotron 3.5 in offline
  mode — serving one model for both paths halves the ops surface.

### What the benchmark must contain (decision gate)
1. ≥ 200 real call segments (both legs, 8 kHz), ~70 % EN / 30 % ES, with human-checked reference
   transcripts, plus the same audio's Cartesia output as the baseline.
2. Targeted slots: names, US states, ages, DOB/dates, phone/ZIP digits, Medicare/plan words, and
   **negations** ("no", "I don't", "not interested") scored separately — a flipped negation is a
   qualification error, not a 1-word WER blip.
3. Silence/hold-music/noise-only clips → hallucinated-word count.
4. Streaming: time-to-final after `finalize`, partial stability (revision rate), p50/p95 latency
   under N concurrent streams, GPU memory per stream.
5. Pass bar: WER and slot accuracy ≤ Cartesia on the same audio, negation errors ≤ Cartesia,
   p95 finalize latency ≤ 300 ms at target concurrency.

---

## 4. Blockers for production (not code)
1. **No GPU is documented on the production Hetzner host** (Hoppwhistle docs list CPU/RAM only).
   Hopvoice already assumes a separate GPU host on a private IP; the STT service should run there
   (or a second GPU box). Size: one L4/L40S-class GPU is expected to cover both paths for
   tens of concurrent calls — must be measured.
2. **No labelled call-audio eval set exists** in either repo.
3. This development container has no GPU, so accuracy/latency/concurrency numbers cannot be
   produced here; the service and tooling can be built and CPU-smoke-tested.
