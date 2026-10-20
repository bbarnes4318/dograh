# AI Voice improvement program: implementation status

This page tracks the voice-improvement program across `bbarnes4318/dograh` and `bbarnes4318/hoppwhistle`.

- Baseline and evidence: [`baseline.md`](./baseline.md). Section numbers below (§n) refer to it.
- Last updated: 2026-10-20 (Task 0).
- Baseline revisions:
  - dograh `9bf1485` (main)
  - pipecat submodule `38bf23d`
  - hoppwhistle `63558a7` (main)

## Status legend

| Status | Meaning |
|---|---|
| `not started` | No work on the task yet. |
| `in progress` | Work is underway. |
| `in review` | A PR is open. |
| `merged` | Merged to the default branch, not yet deployed. |
| `deployed (unverified)` | Rollout performed, but the live checks are not yet done. |
| `verified live` | The live checks in the task's row have passed. |
| `blocked` | Waiting on a dependency or a decision. |

> **Task titles for 1–15 are provisional.** The Task 0 prompt asks this document to track Tasks 1–15 but does not define them, and neither repository contains a task list. The titles and scopes below are derived from the program objectives and from the Task 0 findings. Replace each title and scope with the official wording when that task's prompt is issued, and keep the source-location and live-check columns current.

---

## Task 0: architecture and deployment baseline

| Field | Value |
|---|---|
| Status | `in review`: documentation only, no runtime change |
| Deliverables | `docs/voice-improvements/baseline.md`, this file |
| Pending live verification | Run the redacted checklist in baseline §12 (sections A–G) and record the results below |

### Live baseline record

Fill this in from the checklist. Never paste secrets, phone numbers, or IP addresses.

| Item | Value | Checked by / date |
|---|---|---|
| Running dograh-api image, digest, and revision label | UNKNOWN | |
| Running dograh-ui image (`voicestudio`?) and the commit it was built from | UNKNOWN | |
| Pipecat version and commit in the running image | UNKNOWN | |
| API bind mounts (destinations only) | UNKNOWN | |
| `HOPWHISTLE_*` markers present in the container | UNKNOWN | |
| DB `alembic_version` | UNKNOWN | |
| Org AI mode, plus provider and model IDs (STT, LLM, TTS, realtime) | UNKNOWN | |
| Active workflows using `is_realtime` / `openai_live` | UNKNOWN | |
| Transfer-destination behavior: source order vs forced `DOGRAH_TRANSFER_DESTINATION` vs direct-transfer tools | UNKNOWN | |
| Installed Asterisk `HOPWHISTLE` blocks | UNKNOWN | |
| Hoppwhistle commit | UNKNOWN | |
| `AIVOICE_JWT_SECRET` / `OSS_JWT_SECRET` match (hash prefix) | UNKNOWN | |
| Call-quality bridge scheduled; `ExternalAgentBinding` count | UNKNOWN | |
| `DOGRAH_CALLBACK_BRIDGE` set | UNKNOWN | |

---

## Cross-cutting prerequisites found in Task 0

These are not voice-quality features. They decide whether any later dograh change actually reaches production.

| ID | Finding | Evidence | Blocks |
|---|---|---|---|
| P1 | Production may run a stock upstream image with patch files bind-mounted over the source. A mounted copy hides newer source in the same file. | baseline §10.1, §10.4 | All dograh runtime tasks |
| P2 | The cutover scripts hard-code `NEW_ALEMBIC_HEAD="b7c41d9e52aa"`, but the source head is `c3f9a1d7e4b2`. Running `2_cutover.sh` would migrate the DB, fail its own check, and roll back while leaving the schema changes in place. | `deploy/hopwhistle/cutover/common.sh:19`, `2_cutover.sh:121`; baseline §10.5 | Task 14 |
| P3 | The ARI trunk, dial-prefix, dial-format, and per-caller-ID trunk patch is **not** in source. Removing the `provider.py` mount would silently drop that routing. | baseline §10.4 | Tasks 9, 14 |
| P4 | The forced shared transfer DID (`DOGRAH_TRANSFER_DESTINATION`) exists only in a Hoppwhistle patch copy. Source uses lead → campaign → `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION`. | `hw: deploy/dograh/patches/campaign_call_dispatcher.py:35-36,402-405`; `dograh: api/services/campaign/campaign_call_dispatcher.py:87-98` | Tasks 9, 14 |

---

## Tasks 1–15 (provisional)

| # | Provisional title | Scope from the Task 0 findings | Depends on | Primary source locations | Pending live verification | Status |
|---|---|---|---|---|---|---|
| 1 | Latency observability | Persist per-turn latency for every path:<br>• STT and TTS TTFB (today only LLM TTFB is stored)<br>• OpenAI Realtime / GPT-Live `VoiceLatencyTracker` timings (today logs only)<br>• expose latency in the public usage response<br>• fill the missing STT usage bucket | 0 | `api/services/pipecat/voice_latency.py`, `realtime_feedback_observer.py:227-253`, `call_metrics.py`, `pipeline_metrics_aggregator.py:34-50`, `api/services/workflow/run_usage_response.py:4-13` | Compare measured p50/p95 against recordings on staged calls | not started |
| 2 | Chained-pipeline response latency | Make the hard-coded VAD `stop_secs=0.2` configurable, plus turn-stop strategy and smart-turn settings. Measure TTS/LLM first-byte times. Keep the defaults unchanged. | 1 | `run_pipeline.py:130-215, 238, 944`, `api/schemas/workflow_configurations.py` | A/B staged calls; audible gap measured from the recording | not started |
| 3 | Reliable interruptions (chained) | Check how the mute strategies, `allow_interrupt`, the output gate (which `transport.output()` frames bypass) and the turn-start strategies interact. | 1 | `run_pipeline.py:162-192, 270-292`, `output_gate.py`, `pipecat_engine.py:1149-1196` | Scripted barge-in calls at each node type | not started |
| 4 | Greeting ownership, caller muting, and buffered speech | Check:<br>• the three greeting modes and greeting suppression<br>• `MuteUntilFirstBotComplete`<br>• `MutedSpeechBufferProcessor` (it relies on the private `_user_is_muted`)<br>• the "waiting" mute state set by dropped speech | 0 | `pipecat_engine.py:891-965, 1149-1196`, `muted_speech_buffer.py`, `run_pipeline.py:1045-1074` | Staged calls where the caller speaks first, speaks during the greeting, and speaks while muted | not started |
| 5 | GPT-Live hardening | Fix:<br>• no `InterruptionFrame` on barge-in<br>• late `UserStartedSpeakingFrame`<br>• audio-file greeting invisible to the model (possible double greeting)<br>• `LLMContextFrame` forwarding<br>• vendored protocol drift | 1, 4 | `api/services/pipecat/realtime/openai_live/*`, `run_pipeline.py:216-235, 283-291`, `pipecat/.../external_user_turn_start_strategy.py:23` | Staged GPT-Live calls with barge-in, an audio greeting, transitions, and tools. **The live protocol and model ID are unverified.** | not started |
| 6 | Other speech-to-speech parity | Engine `TTSSpeakFrame`s are dropped after the opening on OpenAI Realtime, Azure, Grok, Gemini, and Ultravox, which loses transition lines, nudges, fillers, and pre-messages. There is no output gate on Azure, Grok, Gemini, or Ultravox. | 4 | `realtime/openai_realtime.py:216-223`, `azure_realtime.py:73-74`, `grok_realtime.py:75-76`, `gemini_live.py:282-287`, `ultravox_realtime.py:99-104` | Only for providers actually selected (see the live baseline record) | not started |
| 7 | Natural delivery (TTS) | Voice, speed, and text-filter behavior (`build_tts_text_filter`); ambient noise; thinking-cue phrasing; the Fish shim versus installing the `fish` extra. | 0 | `service_factory.py:544-`, `call_hygiene.py:264-266`, `thinking_cue.py`, `fish_msgpack_shim.py`, `api/Dockerfile:38-43` | Listening review of anonymized staged recordings | not started |
| 8 | Understanding accuracy (STT) | Keyterms (`dictionary`); STT provider and model choice; model-ID validation (today model IDs are free text). Confirm that no workflow's selected provider changes. | 0 | `registry.py`, `check_validity.py`, `run_pipeline.py:662-667, 721` | WER on anonymized fixtures; staged calls | not started |
| 9 | Dependable tools and transfers | Transfer must stay app-authoritative:<br>• one documented destination authority (P4)<br>• ARI trunk support in source (P3)<br>• caller-ID handling for the transfer leg (`ARI_TRANSFER_DEFAULT_CALLER_ID` is always used)<br>• timeout and failure handling<br>• HTTP/MCP tool timeouts | 0, P3, P4 | `pipecat_engine_custom_tools.py:833-1316`, `ari/provider.py:429-547`, `ari/strategies.py`, `transfer_resolver.py`, `ari_manager.py:1042-1156` | Staged transfer to a test DID; confirm the destination answers, two-way audio, and the max-duration timer is disarmed. **This crosses the Hoppwhistle FreeSWITCH protected area.** | not started |
| 10 | Qualification state authority | Keep qualification state in the application. Today it lives in node extraction plus end-node `USER_QUALIFIED`, and transfer is gated by the model's tool call. Add app-side checks before a transfer or other external action. | 9 | `pipecat_engine.py:476-478, 601-726, 977-1035`, `pipecat_engine_variable_extractor.py` | Staged calls with qualifying and non-qualifying scripts. **Must not alter campaign qualification requirements without approval.** | not started |
| 11 | Call outcome correctness (ARI) | ARI campaign calls get no status updates, so there are no busy/no-answer retries, the circuit breaker sees no answer outcomes, and unanswered runs are never marked completed. `retry_on_voicemail` is never triggered. Twilio/Vonage AMD results are unused. | 0 | `api/services/telephony/status_processor.py`, `ari_manager.py:393-420`, `campaign_orchestrator.py:185-309` | Staged no-answer and busy calls on ARI | not started |
| 12 | Configuration safety | Mask `voicemail_detection.api_key` in workflow responses. Mask Langfuse credentials if they are exposed. Validated config with rollback for new behavior. | 0 | `api/services/configuration/masking.py:147-215`, `routes/organization.py:1174` | API response inspection on staging | not started |
| 13 | Hoppwhistle quality ingestion | Schedule the run bridge (or replace it with Dograh webhook delivery). Remove the transcription-key gate for already-transcribed Dograh runs. Bindings and attribution. Feed latency into grading. | 1 | `hw: deploy/dograh/call-quality-bridge/push_runs.py`, `hw: apps/api/src/services/call-quality/{config,dograh-ingest,jobs}.ts` | Confirm a signed POST is accepted, the run is graded, and quarantine behaves correctly on staging | not started |
| 14 | Deployment convergence | Move from patch mounts to a source-built, revision-labelled image:<br>• fix P2<br>• carry P3 and P4 into source behind config<br>• retire redundant mounts and duplicate indexes<br>• build the UI from a known commit | 0, P1–P4 | `deploy/hopwhistle/cutover/*`, `hw: deploy/dograh/*`, `api/Dockerfile`, `.github/workflows/docker-image.yml` | Server checklist A–E before and after. Rollback image tag recorded. **No deploy as part of the prompts.** | not started |
| 15 | Regression and evaluation harness | Mocked-provider pipeline tests and synthetic, anonymized conversation fixtures covering latency, barge-in, greeting, transfer, and qualification. A per-task acceptance gate. | 1 | `api/tests/`, `evals/` | n/a (offline); live checks belong to each task | not started |

### Dependency summary

```
0 ─┬─ P1..P4 ─ 14
   ├─ 1 ─┬─ 2 ─ 3
   │     ├─ 5 (also needs 4)
   │     ├─ 13
   │     └─ 15
   ├─ 4 ─┬─ 5
   │     └─ 6
   ├─ 7, 8, 11, 12
   └─ 9 (needs P3, P4) ─ 10
```

---

## Cross-repository changes and rollout order

| Change | dograh | hoppwhistle | Order |
|---|---|---|---|
| Transfer destination authority (P4) | Source dispatcher plus config | Patch copy retired; `direct-transfer` tool rewrite documented | dograh first, then remove the hw mount |
| ARI trunk routing (P3) | Port to `ari/provider.py` behind env | `ari-trunk` / `vonage-trunk` kits retired | dograh image first, then drop the mount |
| Quality ingestion (Task 13) | Optional webhook or latency fields | Ingest gate, bridge schedule | hw first if the payload is additive |
| SSO / agency mapping | none in scope | protected files (`aivoice.ts`, `aivoice-jwt.ts`) | requires explicit authorization |

## Protected areas to respect

- **Hoppwhistle** (`hw: .agents/AGENTS.md`; `hw: docs/saas/PROTECTED_SYSTEM_INVARIANTS.md`):
  - `apps/freeswitch/**`
  - `apps/kamailio/**`
  - `apps/rtpengine/**`
  - `packages/routing-dsl/**`
  - `routes/agent-phone.ts`, `did-routes.ts`, `aivoice.ts`
  - `lib/aivoice-jwt.ts`
  - the recording, carrier, and routing services
  - the telephony change-control gate (staging, config diff, test call, two-way audio, recording, rollback, approval)
- **Dograh:** no protected-file list was found in this repository.
