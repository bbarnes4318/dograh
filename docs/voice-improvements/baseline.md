# AI Voice baseline: architecture, execution paths, and deployment (Task 0)

Status: read-only source audit. No runtime behavior was changed to produce this document.
Audit date: 2026-10-20.

Every statement below is labelled with one of these categories:

| Label | Meaning |
|---|---|
| **SOURCE** | Implemented in the current source at the commit listed below, and on by default. |
| **CONFIG** | Implemented in source, but on only when a config row, workflow setting, or env var enables it. |
| **TOOLING** | A deployment or patch script exists in a repo. Whether it has been applied in production is not known from source. |
| **LEGACY** | Present in source but dormant, superseded, or unreachable. |
| **UNKNOWN** | Cannot be decided without server or database access. See §11. |

Paths are repository-relative: `dograh:` = `bbarnes4318/dograh`, `hw:` = `bbarnes4318/hoppwhistle`. Line numbers are as of the commits below.

Redaction rule: this document records env var **names** and file paths only. It does not record secrets, phone numbers, IP addresses, customer data, or transcripts.

---

## 1. Revisions audited

| Item | Value |
|---|---|
| dograh branch | `claude/quirky-pasteur-u0uoev`, identical to `origin/main` at audit time |
| dograh commit | `9bf14859a60be276e04e52ac30211432f2a7e073` (2026-10-19, merge of PR #52) |
| dograh worktree | clean (fresh clone; the only new files are these docs) |
| Pipecat submodule | `https://github.com/dograh-hq/pipecat.git` @ `38bf23d0432e022dd9ea8f60460eea2109758750` (2026-07-25, "fix: ignore function calls in classifier branch") |
| Pipecat version string | `setuptools_scm` with `fallback_version = "0.0.0-dev"` (`pipecat/pyproject.toml`). The shallow submodule has no tags, so an image built from it probably reports `0.0.0-dev` (UNKNOWN). The newest CHANGELOG entry is 1.5.0 (2026-07-04). |
| hoppwhistle branch | `main` |
| hoppwhistle commit | `63558a7d57208df6193c947bd32ae24bcb8eb8ad` (2026-10-20, merge of PR #382). Shallow clone, depth 1. |
| hoppwhistle worktree | clean (fresh clone) |

These are the **repository** revisions. The production revisions are not known. The evidence (§10) suggests production may run a stock upstream image with patch files bind-mounted over it, not this commit. Use the checklist in §12 to establish the running revisions.

---

## 2. System at a glance

```
Hoppwhistle (hopwhistle.com)                          Dograh "AI Voice" box (/opt/dograh, <host>)
├─ /voice-agents iframe ── SSO cookie (JWT HS256) ──▶ dograh-ui (Next.js) ─▶ dograh-api (FastAPI, :8000)
├─ FreeSWITCH (protected)                               │  one container runs: uvicorn workers,
│   ├─ inbound DID ─▶ /api/v1/freeswitch/lookup         │  ari_manager, campaign_orchestrator, arq
│   │   └─ AI_CALLBACK ─▶ bridge to Dograh Asterisk ───▶├─ dograh-asterisk (host network, PJSIP, ARI)
│   └─ "inbound-ai-transfer" (DID*called*caller*dir) ◀──┤   ARI bridge-swap transfer leg
├─ call-quality ingest ◀── HMAC POST ── push_runs.py (cron?, runs in dograh-api-1, reads Dograh PG)
└─ deploy/dograh/* kits ──(docker cp / bind mounts / SQL / Asterisk conf)──▶ the Dograh box
```

**SOURCE:** the live telephony provider for Hoppwhistle is **Asterisk ARI**. Evidence:
- `dograh: deploy/hopwhistle/cutover/README.md:5-11`
- Hoppwhistle kits target `dograh-asterisk`
- FreeSWITCH dialplan bridges to Dograh's Asterisk

There are six other provider packages (Twilio, Vonage, Telnyx, Cloudonix, Plivo, Vobiz). All are registered (`dograh: api/services/telephony/providers/__init__.py:9-17`), but there is no evidence they carry Hoppwhistle traffic (UNKNOWN; likely dormant for this deployment).

---

## 3. Outbound: answer → audio transport → agent startup → conversation → transfer/end

### 3.1 Initiation

1. **Campaign path** (SOURCE; production path)
   1. `POST /api/v1/campaign/{id}/start` (`api/routes/campaign.py:538`) calls `CampaignRunnerService.start_campaign` (`api/services/campaign/runner.py:32-62`), which queues the `SYNC_CAMPAIGN_SOURCE` job.
   2. The standalone `campaign_orchestrator` process (`scripts/run_campaign_orchestrator.sh`) picks it up and runs `_schedule_next_batch` (`api/services/campaign/campaign_orchestrator.py:374-459`). This applies the time window and circuit breaker, then queues `PROCESS_CAMPAIGN_BATCH`.
   3. An ARQ worker runs `process_campaign_batch` (`api/tasks/campaign_tasks.py:99`), which calls `CampaignCallDispatcher.process_batch` (`api/services/campaign/campaign_call_dispatcher.py:135`). For each lead:
      - check the lead's local calling window (`:185`);
      - take a token-bucket rate-limit token (`:673-702`);
      - take an org/campaign concurrency slot (`:704-749`);
      - call `dispatch_call` (`:354`).
   4. `dispatch_call` does the following:
      - resolves the telephony config (`:112-129`);
      - selects a caller ID by local presence and state, using `DOGRAH_STATE_CID_POLICY` or campaign `orchestrator_metadata.state_cid_policy` (`:66-84, 392-478`);
      - writes `initial_context.transfer_destination` from `resolve_transfer_destination`, which tries the lead's `context_variables` first, then campaign `orchestrator_metadata`, then env `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION` (`:87-98, 503-505`);
      - creates the workflow run (`:512`) and checks quota (`:557`);
      - calls `provider.initiate_call` (`:591`);
      - for ARI, stores a Redis channel→run mapping (`:618-628`).
2. **Test call** (SOURCE): `POST /api/v1/telephony/initiate-call` (`api/routes/telephony.py:78-286`).
3. **API trigger** (SOURCE): `POST /api/v1/public/agent/{uuid}` (`api/routes/public_agent.py:405-470`).

**Contradiction (TOOLING vs SOURCE):** `hw: deploy/dograh/patches/campaign_call_dispatcher.py:35-36,402-405` is a copy of the dispatcher meant to be bind-mounted. It **forces every transfer to one shared DID**, read from env `DOGRAH_TRANSFER_DESTINATION`, with a hard-coded fallback, and ignores lead and campaign values.
- Current dograh source does **not** force a shared DID. It uses the lead → campaign → `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION` order.
- Which behavior is live depends on whether that mount is present (UNKNOWN, §12).
- The env var names also differ: `DOGRAH_TRANSFER_DESTINATION` in the patch, `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION` in source.

### 3.2 Dial, answer, and media (ARI)

1. `ARIProvider.initiate_call` POSTs to `/ari/channels` (`api/services/telephony/providers/ari/provider.py:80-166`).
   - Endpoint: `PJSIP/<num>[@trunk]`, where the trunk comes from env `ARI_PJSIP_DEFAULT_TRUNK` (`:27-36`).
   - Stasis app arguments: `workflow_run_id=…,workflow_id=…`.
   - ARI has no HTTP status callbacks.
2. `ari_manager` (`api/services/telephony/ari_manager.py`, a standalone process started by `scripts/run_ari_manager.sh`) keeps one events websocket per ARI config (`:174-278`) and reloads configs every 60 s (`:1182-1340`).
3. **Answer:** a `StasisStart` event whose state is not `Ring` is an answered outbound call (`:337-375`). `_handle_stasis_start` (`:730`) then:
   - stores the Redis mappings;
   - creates an external-media channel with `transport=websocket, format=ulaw` (`:508-556`);
   - creates a mixing bridge for the caller and the media channel (`:300-321, 812-845`).
4. **Media:** Asterisk connects to `WS /api/v1/telephony/ws/ari?...` (`api/routes/telephony.py:550-743`).
   - The handler requires the run to be in state `initialized` (`:638`).
   - ARI websocket authentication always passes (`providers/base.py:222-241`). The handler's own docstring notes the socket is unauthenticated (`telephony.py:594-601`).
   - The handler then calls `ARIProvider.handle_websocket` → `run_pipeline_telephony(...)` (`ari/provider.py:255-290`).
5. **Transport:** `FastAPIWebsocketTransport` with `AsteriskFrameSerializer`, using `ARIBridgeSwapStrategy` for transfers and `ARIHangupStrategy` for hangups (`api/services/telephony/providers/ari/transport.py:19-75`). Audio runs at 8 kHz (`ari/__init__.py:159`, `api/services/pipecat/audio_config.py:73-105`).

**Answering-machine detection:**
- Carrier AMD exists only for Twilio and Vonage (`amd_enabled`), and its result (`gathered_context.answered_by`) is never read.
- For ARI, detection is in-pipeline only:
  - `VoicemailDetector`: CONFIG, chained pipelines only (`run_pipeline.py:1150-1189`).
  - `MachineAnswerGuard` (`call_hygiene.py`): on by default for chained pipelines, with deployment kill switch `CALL_HYGIENE_ENABLED`.

### 3.3 Agent startup

- `run_pipeline_telephony` (`api/services/pipecat/run_pipeline.py:295-323`) registers the call in the per-process active-call set and releases the concurrency slot in `finally`.
- `_run_pipeline_telephony_impl` (`:326-433`) loads the workflow and **pinned definition**, then resolves the AI config (`:641-679`; see §7).
- It stamps `initial_context.runtime_configuration` (`:736-760`), builds services (`service_factory.py`), the engine (`api/services/workflow/pipecat_engine.py`) and the pipeline (`pipeline_builder.py`).
- It then runs `run_pipeline_worker` (`worker_runner.py:6-16`, called at `run_pipeline.py:1332`).
- **Greeting:** after `on_client_connected` and the optional pre-call fetch (a ringer plays meanwhile), the engine runs `set_node(start)` then `queue_node_opening(generate_if_no_greeting=True)` (`api/services/pipecat/event_handlers.py:108-171`). See §6.2.

### 3.4 Transfer and end (ARI)

**Transfer**
1. The transfer tool handler is in `api/services/workflow/pipecat_engine_custom_tools.py:833-1264`. It:
   - blocks if call hygiene saw a machine, screener, or bot (`:851-878`);
   - resolves the destination: static template, dynamic HTTP resolver, or context mapping (`api/services/workflow/tools/transfer_resolver.py`);
   - stores the transfer context in Redis;
   - mutes the pipeline and **disarms the max-duration timer** (`:1112-1119`; marker `HOPWHISTLE_TRANSFER_DURATION_HOTFIX_V1`, now in source);
   - calls `ARIProvider.transfer_call` (`ari/provider.py:429-547`). This dials the destination into Stasis with `appArgs=transfer,<id>`, using caller ID from env `ARI_TRANSFER_DEFAULT_CALLER_ID` (the tool never passes `caller_id`).
2. Hold music plays while the handler waits on Redis pub/sub (`call_transfer_manager.py:122-198`).
3. On `destination_answered`, the tool ends with `EndFrame(reason="transfer_call")`. The serializer then runs `ARIBridgeSwapStrategy` (`pipecat/src/pipecat/serializers/asterisk.py:112-150`; `ari/strategies.py:15-200`), which adds the destination to the bridge and hangs up the media channel.
4. On timeout or failure, the tool calls `cancel_transfer_call`.
5. **CONFIG:** if the call came from an external PBX (VICIdial headers), the transfer is a VICIdial `INGROUPTRANSFER` instead (`ari/provider.py:382-427`, `ari/external_pbx/vicidial.py:50-108`).

**Where the transferred leg goes:** the transfer leg reaches Hoppwhistle FreeSWITCH, either via the FracTEL PSTN round-trip or via the `direct-transfer` kit's `PJSIP/hopwhistle/sip:<DID>*called*caller*dir@...` destination (TOOLING; it rewrites the Dograh `tools` table). FreeSWITCH `public.xml` `inbound-ai-transfer` → `inbound_route.lua` → `/api/v1/freeswitch/lookup?...&via=dograh` routes it like a normal inbound call on the campaign DID (agents, shared group, or buyer). See §9.3.

**End**
- `end_call_with_reason` (`pipecat_engine.py:977-1035`): runs final extraction, persists the disposition, then queues `EndFrame` or `CancelFrame`.
- The other ways a call ends:
  - end-call tool (`custom_tools.py:768-831`);
  - hygiene (`pipecat_engine.py:1037-1062`);
  - max duration (`pipecat_engine_callbacks.py:257-279`; suppressed once a transfer starts);
  - idle ladder (`pipecat_engine.py:1250-1277`);
  - caller hangup (`event_handlers.py:180-192`).
- `ari_manager` StasisEnd frees the slot and caller ID and tears down the bridge and channels (`ari_manager.py:847-1015`).

**Gaps found (SOURCE, by reading; not tested):**
- For ARI, `_process_status_update` is never called. The only callers are HTTP-webhook provider routes. As a result:
  - there are no busy/no-answer retry events for ARI campaigns;
  - the circuit breaker never records answer outcomes;
  - unanswered runs are not marked completed.
- `retry_on_voicemail` is checked by the orchestrator, but nothing publishes a `voicemail` reason (LEGACY in effect).

---

## 4. Inbound: callback → workflow selection → conversation → human handoff

### 4.1 Dograh side

**ARI (SOURCE, live provider)**
- A `StasisStart` in state `Ring` runs `_handle_inbound_stasis_start` (`ari_manager.py:582-710`).
- It calls `find_active_phone_number_for_inbound(org, exten, "ari")`. **The workflow is `telephony_phone_numbers.inbound_workflow_id`** (`api/db/models.py:279`); if none is set, the call is hung up.
- It then takes a slot, reads external-PBX headers, creates the run (`initial_context.external_pbx_call`), checks quota, answers, and runs the same media setup as outbound.

**HTTP providers (SOURCE, likely dormant here)**
- `POST /api/v1/telephony/inbound/run` (`api/routes/telephony.py:746-905`).
- The legacy `POST /inbound/{workflow_id}` route is deprecated (LEGACY).

**Handoff:** the same transfer tool as outbound (§3.4).

**Not in Dograh source:** no "callback" concept links an inbound call to an earlier outbound run. A callback is just an inbound call to a number whose `inbound_workflow_id` is set.

### 4.2 Hoppwhistle side (the "inbound callback" feature)

**CONFIG + TOOLING**
- `hw: apps/api/src/services/dograh-callback-routing.ts` is off unless env `DOGRAH_CALLBACK_BRIDGE` holds a `{DID}` template (`:16-18, 70-74`).
- `findDograhCallbackRoute` (`:76-110`) requires an ACTIVE `PhoneNumber` with `metadata.dograhCallback=true` that is not assigned to a user or campaign and is not a transfer destination. `findUnroutedCallbackRoute` (`:120-145`) also sends DIDs with no route to the AI.
- `/api/v1/freeswitch/lookup` (`hw: apps/api/src/routes/did-routes.ts:519-723`; protected file) checks, in order: TCPA litigator, AI-callback (skipped when `via=dograh`), RTB lease, `DidRoute`. An AI callback is answered with `routeType: 'AI_CALLBACK'` and `aiAgentBridge`.
- FreeSWITCH `inbound_route.lua:655-679` (protected) bridges the call to Dograh Asterisk.
- The Asterisk context `dograh-ai-callback`, installed by `hw: deploy/dograh/inbound-callback/install_inbound_callback.py`, sends the call into Stasis.
- Dograh then selects the agent by `inbound_workflow_id`, which is written by `assign_inbound_agent.py:150`.
- `hw: deploy/dograh/inbound-callback/README.md:94-95` says the kit was not tested against live Asterisk/Dograh. Live status: UNKNOWN.

---

## 5. Chained STT → LLM → TTS execution

**SOURCE.** Assembled by `api/services/pipecat/pipeline_builder.py:73-127` (called at `run_pipeline.py:1222-1239`). Processor order:

```
transport.input → [DTMFCapture] → STT → [MachineAnswerGuard] → [VoicemailDetector.detector]
→ [MutedSpeechBuffer] → user aggregator → [voicemail llm_gate] → LLM → ConversationOutputGate
→ [AssistantTurnGuard] → PipelineEngineCallbacksProcessor → [RecordingRouter] → TTS
→ transport.output → AudioBufferProcessor → assistant aggregator → PipelineMetricsAggregator
```

### VAD

- `SileroVADAnalyzer(VADParams(stop_secs=0.2))`, hard-coded (`run_pipeline.py:238, 944`).
- No per-workflow VAD confidence, start_secs, or volume settings.

### Turn start

`turn_start_strategy` (`run_pipeline.py:162-192`; schema `api/schemas/workflow_configurations.py:210-214`):

| Value | Strategy |
|---|---|
| `default` (default) | Transcription + VAD |
| `min_words` | 3 words |
| `provisional_vad` | 1.5 s |

STT services with their own turn detection (Deepgram Flux, Dograh Flux languages, Cartesia `ink-2`; `service_factory.py:173-180`) use `ExternalUserTurnStartStrategy(enable_interruptions=True)`.

### Turn stop

`turn_stop_strategy` (`run_pipeline.py:195-215`):
- `transcription` (default): `SpeechTimeoutUserTurnStopStrategy`.
- `turn_analyzer`: `LocalSmartTurnAnalyzerV3` with `smart_turn_stop_secs`, default 2.0.

### Interruptions

There is no `allow_interruptions` flag. Interruptions are governed by:
- the turn-start strategies;
- the mute strategies (`run_pipeline.py:270-292`):
  - `FunctionCallUserMuteStrategy`;
  - `CallbackUserMuteStrategy(engine.should_mute_user)`;
  - `MuteUntilFirstBotCompleteUserMuteStrategy`, except for GPT-Live or when the start node has `allow_interrupt=True`;
- the per-node `allow_interrupt` setting.

### Output gate

`output_gate.py` drops `LLMTextFrame`s while suppressed (terminal tool chains, media playback). Frames queued directly to `transport.output()` bypass it.

### Services

| Kind | Providers | Location |
|---|---|---|
| STT | 14 | `service_factory.py:242-541` |
| TTS | 17, including Fish via the fork-local `fish_msgpack_shim.py` | `service_factory.py:544-` |
| LLM | 13 | `service_factory.py:980-1140` |

The full registry is in `api/services/configuration/registry.py`; see §7.

### Other per-call helpers (SOURCE)

| Helper | What it does | Status |
|---|---|---|
| `thinking_cue.py` | Delayed filler `TTSSpeakFrame` during HTTP/MCP tools | `tool_filler` CONFIG |
| `call_hygiene.py` | Machine/screener/bot guards; strips markup from TTS text | chained only |
| `audio_mixer.py` | Ambient noise | CONFIG |
| `recording_router_processor.py` | ● pre-recorded clip vs ▸ TTS markers | CONFIG, when the org has recordings |

---

## 6. Speech-to-speech paths, node transitions, greeting, muting, buffered speech, tools

### 6.1 Speech-to-speech execution paths

**How S2S is selected:**
- `is_realtime = user_config.is_realtime and user_config.realtime is not None` (`run_pipeline.py:701`).
- This can be set per workflow (`api/services/configuration/resolve.py:96-98`).
- Pipeline: `pipeline_builder.py:168-196`.

**Features disabled under S2S:** voicemail detector, recording router, muted-speech buffer, call hygiene, context compaction.

**Variable extraction under S2S:** uses a separate text `inference_llm` (`run_pipeline.py:713-716`).

| Provider id | Dograh class | Pipecat base (pinned fork) | Turn detection | Output gate | Status |
|---|---|---|---|---|---|
| `openai_realtime` | `realtime/openai_realtime.py:62` | `services/openai/realtime/llm.py:209` | provider (External start/stop) | yes | SOURCE, complete |
| **`openai_live` (GPT-Live)** | `realtime/openai_live/service.py:170` | **none.** The protocol is vendored (`openai_live/events.py:9-13`); the fork has no `services/openai/live` | External start **without interruptions** (`run_pipeline.py:224`) | yes | SOURCE, functionally complete, with gaps (below) |
| `azure_realtime` | `realtime/azure_realtime.py:35` | `services/azure/realtime/llm.py:24` | provider | **no** | SOURCE |
| `grok_realtime` | `realtime/grok_realtime.py:46` | `services/xai/realtime/llm.py:181` | provider | **no** | SOURCE |
| `google_realtime` / `google_vertex_realtime` | `realtime/gemini_live.py:45`, `gemini_live_vertex.py:24` | `services/google/gemini_live/llm.py:377` (+ vertex) | local Silero, interruptions **off** | **no** | SOURCE; the session reconnects on every node transition |
| `ultravox_realtime` | `realtime/ultravox_realtime.py:56` | `services/ultravox/llm.py:166` | local Silero, interruptions on | **no** | SOURCE; native `new-stage` transitions |
| AWS Nova Sonic | — | exists in the fork | — | — | LEGACY / not wired (no `api/` reference; extra not installed) |

#### GPT-Live (`openai_live`, model `gpt-live-1`)

Built at `service_factory.py:1206-1225`.

**Architecture:**
- The service speaks a WebSocket protocol directly to `wss://api.openai.com/v1/live/sessions` (`service.py:98, 455-473`).
- The *frontend* model gets voice/persona instructions only (`:124-151`).
- A delegated *backend* model (default `gpt-4.1`) gets the node prompt and the Responses-format tools (`:387-417`; `adapter.py:60-134`).
- `session.start` waits for a system instruction. Node changes send `session.update` (`:322-328, 419-446`).

**Tools:**
- Backend `function_call` items become `FunctionCallFromLLM` and run through the engine handlers.
- Transition calls are deferred while the bot is speaking, capped at 6 s (`:108, 888-946`).
- Results go back as `function_call_output` + `response.create` (`:948-1003`).
- `run_llm=False` keeps the model silent until the caller speaks (`:957-961`).

**Greeting and engine lines:**
- A static greeting `TTSSpeakFrame` becomes `session.commentary.append` (`:807-822`).
- With no greeting, the model gets `OPENING_PROMPT` (`:824-841`).
- GPT-Live is the **only** S2S path that speaks engine lines after the opening (transition text, nudges).

**Gaps (by reading; impact UNKNOWN until live tests are run):**
1. **No `InterruptionFrame` on barge-in.** `ExternalUserTurnStartStrategy()` defaults to `enable_interruptions=False` (`pipecat/src/pipecat/turns/user_start/external_user_turn_start_strategy.py:23`). Audio already queued in the transport may therefore keep playing after the caller starts talking.
2. **Late `UserStartedSpeakingFrame`.** It is only emitted when the first input-transcript delta arrives (`service.py:741-749`).
3. **Audio-file greeting bypasses the model.** The audio greeting is played directly to the transport (`pipecat_engine.py:919-939`), so GPT-Live never sees it. A later `LLMContextFrame` may then trigger `OPENING_PROMPT`, a possible double greeting.
4. **`LLMContextFrame` is forwarded downstream** (`service.py:363`), which is not the "absorbed" contract that `pipeline_builder.py:149-160` assumes.
5. **Vendored protocol.** The protocol models are hand-vendored "from pipecat-ai 1.12" and will drift from the real API.

Tests: `api/tests/test_openai_live_service.py`, run with mocks; no live validation.

#### Other S2S paths (OpenAI Realtime, Azure, Grok, Gemini, Ultravox)

**Gap: engine lines are dropped after the opening.** Every `TTSSpeakFrame` after the opening is dropped with a warning (e.g. `realtime/openai_realtime.py:216-223`). That silently discards:
- transition speech;
- HTTP-tool pre-messages;
- thinking cues;
- canned idle nudges.

Those code paths also set `_queued_speech_mute_state="waiting"`, which can hold the caller muted until the next bot start/stop (`pipecat_engine.py:1168-1186`). This is an inferred risk.

### 6.2 Node transitions, greeting ownership, muting, buffered speech, tools (SOURCE)

#### Node transitions

**Transitions are LLM function calls.** Each outgoing edge becomes a tool named from its label, with the edge condition as the description (`workflow_graph.py:93-94`, `pipecat_engine_context_composer.py:153-164`), registered with `is_node_transition=True` (`pipecat_engine.py:523-527`).

The handler (`pipecat_engine.py:376-497`):
1. starts background variable extraction for the node being left;
2. queues transition speech (a recording, configured text, or the model's `transition_message` when `speak_during_transition` is on; CONFIG, default off);
3. runs `set_node` (re-registers tools, recomposes the prompt, calls `llm._update_settings`);
4. returns `{"status":"done"}` with an `on_context_updated` hook that ends the call when the new node is an end node.

Fork-specific gating: `NodeTransitionStartedFrame`, and `LLMService._function_is_node_transition` (`pipecat/src/pipecat/services/llm_service.py:1228-1285`).

#### Greeting ownership

`queue_node_opening` (`pipecat_engine.py:908-965`) picks one of:

| Opening | How it is delivered |
|---|---|
| Audio greeting | `play_audio` straight to `transport.output()` (`audio_playback.py:93-139`) |
| Static text | `TTSSpeakFrame(append_to_context=True)` |
| Neither | LLM-generated via `LLMContextFrame` |

- The greeting is skipped if the caller already spoke (`:891-897, 1227-1234`).
- Greeting text is rendered with call context.

#### Caller muting

`should_mute_user` (`pipecat_engine.py:1149-1196`) mutes the caller during any of:
- shutdown;
- queued speech (waiting or playing);
- bot speech on an `allow_interrupt=False` node.

It is combined with the function-call mute and the first-bot-turn mute. Transfers set `set_mute_pipeline(True)`.

#### Buffered speech

`MutedSpeechBufferProcessor` (`api/services/pipecat/muted_speech_buffer.py`): CONFIG `buffer_muted_speech`, default **True**; chained pipelines only.
- **Capture:** it swallows final transcripts while the aggregator is muted. It reads the private `_user_is_muted` attribute (`run_pipeline.py:1072-1074`).
- **Replay:** after unmute it replays the tail (≤600 chars, ≤45 s old) as `LLMMessagesAppendFrame` with the prefix `"[The caller said this while you were still speaking]"`.
  - `run_llm=False` when a transition will generate anyway.
  - It forces `run_llm=True` if no generation starts within 1.5 s (`:45-57, 188-264`).

#### Tools

Built in `pipecat_engine_custom_tools.py:356-394`:

| Tool | Behavior | Location |
|---|---|---|
| HTTP | optional pre-message, thinking cue, 5 s default timeout | `:434-511` |
| MCP | per-call `McpToolSession`; degrades when the server is down | `mcp_tool_session.py` |
| End-call | ends the call | `:768-831` |
| Transfer | see §3.4 | `:833-1264` |
| Play-audio | plays a clip | `:541-654` |
| Send-SMS | recipient taken from call setup, never from model args | `:656-766` |
| Calculator, knowledge base, send DTMF | built-ins | — |

Results return via `result_callback(..., FunctionCallResultProperties(run_llm, on_context_updated))`.

**Authority note:** for transfers and SMS, the application drives the outcome (provider call plus a Redis event). The model's text is not taken as proof of success.

#### Qualification and extraction

- **Extraction:** per node, CONFIG (`extraction_enabled`). It runs in the background on transition and synchronously at end of call (`pipecat_engine.py:601-726`) via `inference_llm.run_inference` (`pipecat_engine_variable_extractor.py:156-249`).
- **Qualification:** reaching an end node gives `EndTaskReason.USER_QUALIFIED`. The disposition prefers an extracted `call_disposition`.
- **Where the rules live:** the qualification rules are in the workflow prompt and graph, stored in the database. The repo-root export `Final_Expense_Alex_Live_Transfer.json` is an example, not proof of the live definition.

---

## 7. Provider selection, model validation, credentials, per-workflow config

### Org configuration

SOURCE. `organization_configurations[MODEL_CONFIGURATION_V2]` (`api/services/configuration/ai_model_configuration.py:130-138`; schema `api/schemas/ai_model_configuration.py:52-190`).

Modes:
- `dograh`: managed via MPS (`MPS_API_URL`).
- `byok`, with two sub-modes:
  - `pipeline`: llm/tts/stt/embeddings;
  - `realtime`: realtime + llm + embeddings.

### Workflow configuration

The run uses the **pinned definition** (`run_pipeline.py:641-679`).

| What the workflow sets | Effect |
|---|---|
| `workflow_configurations.model_configuration_v2_override` | **Fully replaces** the org config (`ai_model_configuration.py:97-103`) |
| Legacy `model_overrides` | Merged per section (`resolve.py:76-132`): a different provider replaces the section; the same provider is a field-level merge |

- Embeddings cannot be overridden per workflow.
- There are no per-node model overrides, except the QA node and the voicemail LLM.

### Model IDs

Model IDs are **free text**. Every `model` field is `str` with UI examples and `allow_custom_input` (e.g. `registry.py:415, 486, 1089, 1796`). There is no allowlist check.

Key validation (`check_validity.py:37-542`):
- **Live API checks:** OpenAI-family, Deepgram, Groq, Dograh, Inception, Inworld, xAI, LMNT, Fish (Fish fails only on 401/403).
- **Always pass:** ElevenLabs, Google, Azure, Azure Speech, Azure Realtime, Cartesia, Smallest.

### Credentials

- **Storage:** plain JSON in `organization_configurations.value` and `workflow_configurations`. No application-level encryption was found. DB or volume encryption: UNKNOWN.
- **API key format:** `api_key` may be a list, rotated randomly on every access (`registry.py:166-194`).
- **Masking:** API responses mask everything but the last 4 characters (`masking.py`).
- **Gap:** `voicemail_detection.api_key` (an untyped extra) is **not masked** in workflow responses (`masking.py:153-166` only covers `model_overrides` and the v2 override).

### Selected provider/model in production

The configuration is in the database and is UNKNOWN. Each run records what it actually used in `workflow_runs.initial_context.runtime_configuration`, so the read-only queries in §12 can establish it.

### Per-workflow voice settings

Defined in `api/schemas/workflow_configurations.py:177-232` (`extra="allow"`):

| Setting | Default / notes |
|---|---|
| `max_call_duration` | 300 s, max 1200 |
| `max_user_idle_timeout` | 10 s |
| `turn_start_strategy`, `turn_start_min_words`, `provisional_vad_pause_secs` | see §5 |
| `turn_stop_strategy`, `smart_turn_stop_secs` | see §5 |
| `dictionary` | STT keyterms |
| `context_compaction_enabled` | — |
| `buffer_muted_speech` | True |
| `speak_during_transition` | False |
| `ambient_noise_configuration` | — |
| `tool_filler` | — |
| `idle_behavior.nudges` | — |
| `dtmf` | — |
| `call_hygiene` | — |
| `voicemail_detection` | untyped |
| `transcript_configuration` | untyped |
| per-node `allow_interrupt` | — |

---

## 8. Recording, transcript, latency, usage, and quality reporting

### Recording

SOURCE.
- **Capture:** Pipecat `AudioBufferProcessor` writes to in-memory buffers: mixed, user, and bot tracks; 100 MB cap each; 16-bit WAV at the pipeline rate (`in_memory_buffers.py`).
- **Upload:** at `on_pipeline_finished`, before the completion job is queued (`event_handlers.py:399-458`; `workflow_run_artifacts.py:46-126`). Keys:
  - `recordings/{run}.wav`;
  - `recordings/{run}/user.wav`;
  - `recordings/{run}/bot.wav`.
- **Storage:** MinIO or S3.
- **Hoppwhistle side:** its FreeSWITCH recordings of the transferred leg are a separate system (protected).

### Transcript

- `TranscriptLogCoordinator` builds the transcript.
- It is saved in three places:
  - `transcripts/{run}.txt` (`transcript_url`);
  - `workflow_runs.transcript_text`;
  - the event log `logs.realtime_feedback_events`.

### Latency

| Metric | Source | Where it ends up |
|---|---|---|
| Per-turn user→bot latency | Pipecat `user_bot_latency_observer` | `rtf-latency-measured` events (`run_pipeline.py:1273-1299`) |
| LLM TTFB | — | `realtime_feedback_observer.py:227-253` |
| STT and TTS TTFB | — | **Not persisted** |
| Per-call rollup | `call_metrics.compute_response_metrics` | `usage_info.latency` / `ttfb` / `num_turns` |
| OpenAI Realtime and GPT-Live per-turn timings | `voice_latency.VoiceLatencyTracker` | **Logs only** |

- Tracing: OTel → Langfuse (`tracing_config.py`), with the trace URL in `gathered_context.trace_url`.
- **Gap:** the public usage response (`run_usage_response.py:4-13`) drops latency, TTFB, and `num_turns`.

### Usage and billing

- `PipelineMetricsAggregator` records LLM tokens, TTS characters, and duration.
- **Gap:** the STT usage bucket is declared but never filled (`pipeline_metrics_aggregator.py:34, 46-50`).
- Cost is MPS-owned (`workflow_run_billing.py`). `cost_info` has no current writers (LEGACY).

### QA

- The QA node (CONFIG) adds per-node LLM analysis, with tags, summary, and score in `annotations` (`api/services/workflow/qa/*`, `api/tasks/run_integrations.py`).
- Reports: `api/routes/reports.py` (campaign insights, transfers CSV, funnel, cohorts on `usage_info.latency.p95_seconds`).

### Post-call jobs

ARQ jobs (`api/tasks/arq.py:52-72`):
- `process_workflow_completion` → integrations → MPS usage;
- durable webhook delivery with a 5-minute sweep;
- Tuner and Paygent integrations (CONFIG).

**Dograh source has no Hoppwhistle-specific delivery.**

### Hoppwhistle run ingestion

See §9.4.

---

## 9. Hoppwhistle: SSO, agency ownership, transfer routing, completed-run ingestion

### 9.1 SSO

SOURCE, protected file.
- **Endpoint:** `GET /api/v1/aivoice/session` (`hw: apps/api/src/routes/aivoice.ts:97`; registered at `apps/api/src/index.ts:450-451`).
- **Access:** staff-only (`apps/api/src/lib/staff-only-endpoints.ts:104`, `middleware/staff-only.ts:61-77`).
- **Token:** HS256 JWT `{sub,email,iat,exp}` (`apps/api/src/lib/aivoice-jwt.ts:33-46`), signed with `AIVOICE_JWT_SECRET`. It must equal Dograh `OSS_JWT_SECRET` (UNKNOWN whether it does).
- **Cookies:** `dograh_auth_token` and `dograh_auth_user` on `AIVOICE_COOKIE_DOMAIN` (default `.hopwhistle.com`).
- **Response:** `{url: AIVOICE_URL}`.
- **Web page:** `apps/web/src/app/(dashboard)/voice-agents/page.tsx` loads the iframe with `allow="microphone; autoplay; clipboard-write"`.
- **Known breakage:** SSO is broken when the portal is served from `agents.netenroll.com` because of a cookie/site mismatch (`aivoice.ts:15-66`; the fix is out of repo).

### 9.2 Agency ownership

SOURCE.
- **No per-agency isolation in Dograh.** Every Hoppwhistle user is signed in as Dograh user `AIVOICE_DEFAULT_USER_ID` (default `1`) (`aivoice.ts:72-93`; per-user provisioning is `TODO(phase-3)`).
- **The only ownership link is for grading.** `ExternalAgentBinding` maps a Dograh workflow id to a tenant (`hw: apps/api/prisma/schema.prisma:6489-6511`). Bindings are created by hand with `apps/api/src/cli/call-quality-bindings.ts`.
- **Payload hints are not trusted.** Ingestion drops metadata keys matching `tenant|agency|organization` (`call-quality/dograh-ingest.ts:111`).
- **Product gap:** the `VOICE_AGENTS` upgrade does not unlock the staff-only API, which looks like a mismatch between what is sold and what is reachable.

### 9.3 Transfer routing

- **Who decides to transfer:** the Dograh workflow, via prompt and tool. Hoppwhistle has no "get transfer destination" API.
- **The destination**, depending on deployment:
  - **(a) Source:** template/resolver, with lead → campaign → `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION`.
  - **(b) Patch mount (TOOLING):** forced `DOGRAH_TRANSFER_DESTINATION`.
  - **(c) `direct-transfer` kit (TOOLING):** rewrites `tools.definition` to `PJSIP/hopwhistle/sip:<DID>*{{called_number}}*{{caller_number}}*{{direction}}@<host>:5080` (`hw: deploy/dograh/direct-transfer/*`) and installs the `hopwhistle` PJSIP trunk in Dograh Asterisk.
- **FreeSWITCH side (protected; cited only):**
  - `public.xml:39-48` `inbound-ai-transfer`;
  - `inbound_route.lua:396-416` restores the lead's caller ID;
  - `:540-548` adds `via=dograh`;
  - `/freeswitch/lookup` then routes via `DidRoute`.
- **Caller-ID pool for Dograh outbound calls:**
  - `hw: deploy/dograh/import_state_caller_ids.py` + `sync-dograh-caller-ids.sh` upsert `telephony_phone_numbers.extra_metadata.pool="state_cid"` (data-only, TOOLING).
  - The state policy is in dograh source (`DOGRAH_STATE_CID_POLICY`, default `off`).

### 9.4 Completed-run ingestion and call quality

**Ingest endpoint (SOURCE in hoppwhistle)**
- `POST /api/internal/call-quality/dograh/runs` (`hw: apps/api/src/routes/call-quality.ts:212`).
- Authentication: HMAC-SHA256 over `${ts}.${body}` with `DOGRAH_QUALITY_INGEST_SECRET`, headers `X-Hopwhistle-Signature` / `X-Hopwhistle-Timestamp`, 300 s skew window, 2 MB limit (`services/call-quality/dograh-ingest.ts:23-75`).
- Results: unbound workflows are quarantined; otherwise an `ExternalCallRecord` is created and a PENDING `CallEvaluation` is queued.

**Producer (TOOLING)**
- `hw: deploy/dograh/call-quality-bridge/push_runs.py`, run inside `dograh-api-1`.
- It reads Dograh Postgres `workflow_runs (id, workflow_id, created_at, logs, …)` read-only, rebuilds turns from `logs`, then signs and POSTs.
- **No cron or systemd unit is in either repo**, so whether it runs is UNKNOWN.

**Grader**
- `CALL_QUALITY_PROVIDER` is `anthropic` or `gemini`; `CALL_QUALITY_MODEL` overrides the model (`call-quality/config.ts:9-58`).
- Polled in-process (`background-jobs.ts:43-58`).
- **Gating gap:** the worker requires `CALL_QUALITY_ENABLED`, a provider key, **and** a transcription engine key (`config.ts:78-89`), even though Dograh runs arrive already transcribed.

### 9.5 Legacy (Vapi)

- `hw: apps/api/src/services/ai-campaign-service.ts` and `routes/ai-campaigns.ts` are registered, but they are **LEGACY / dormant**:
  - `startCampaign` only flips status;
  - the webhook is inert without `callRecordId`;
  - nothing writes `ai_campaign_calls`;
  - there is no nav entry.
- `music-console` voice and `vapi-proxy`: live use UNKNOWN.
- Out of scope for the voice-improvement program.

---

## 10. Deployment: bind mounts, copied modules, patch installers, config overrides

### 10.1 What the tooling says production looks like

These are claims from the tooling's own documentation, not verified facts.

- The box `/opt/dograh` runs the stock `dograhai/dograh-api:latest` image, said to be built on 2026-07-22 (`dograh: deploy/hopwhistle/cutover/README.md:3`).
- Patched `.py` files are bind-mounted from `/opt/dograh-patches` via `/opt/dograh/docker-compose.override.yaml`.
- The docs disagree on how many: "20 files" (`cutover/README.md:3-8`) vs "13" (`deploy/netenroll/README.md:64`).
- The UI is `dograh-ui:voicestudio`, built on the box from branch `hopwhistle/customizations` (`cutover/update_ui.sh:349-350`), not from `main`.
- Host nginx (`/etc/nginx/sites-available/aivoice`, not in either repo) fronts the API on :8000, the UI on :3010, and MinIO on :19000.
- The `remote` compose profile is not used, so there is no bundled coturn. PSTN calls do not use TURN.

### 10.2 Everything in one container

The API container entrypoint (`scripts/start_services_docker.sh`) runs, in the same container:
- `alembic upgrade head` (`:30`);
- uvicorn workers;
- `ari_manager`;
- `campaign_orchestrator`;
- `arq` (`:62-76`).

Any bind mount therefore affects all of them.

### 10.3 Pipecat install

| Context | Install method |
|---|---|
| Image | Non-editable `uv pip install '/tmp/pipecat[...]'` from the submodule (`api/Dockerfile:38-43`). The extras list omits `fish`, which is why `fish_msgpack_shim.py` exists. |
| Dev / CI | Editable install (`scripts/setup_pipecat.sh`, `scripts/setup_requirements.sh:83`) |
| `api/requirements.txt` | Does not pin `pipecat-ai` |
| Production | The pipecat commit inside the image is UNKNOWN |

### 10.4 Patch kits vs current source

All Hoppwhistle `deploy/dograh` kits and dograh `deploy/hopwhistle` scripts, compared with the current source:

| Kit | Mechanism | Dograh files overridden | Now in dograh source? |
|---|---|---|---|
| dograh `deploy/hopwhistle/apply_call_duration_patch.py` | edit staged copy + mount | `api/db/workflow_run_client.py`, new `services/workflow/call_duration.py` | Yes, implemented differently. The script's marker check would not find its marker in the client. |
| dograh `apply_inception_patch.py` | in-place edit + `docker exec cat` seeding | registry, check_validity, service_factory, realtime_feedback_observer, qa/llm_config | Yes |
| dograh `apply_insights_duration_patch.py`, `apply_machine_answer_patch.py` | staged `reports.py` mount | `api/routes/reports.py` | Yes |
| hw `patches/{rate_limiter,campaign_call_dispatcher}.py` + `areacode_state.py` | scp + manual read-only mounts | `services/campaign/*` | Mostly. Source is **newer** (rate-limiter day cap, `allowed_numbers`). **The forced shared transfer DID is not in source.** A mount would hide newer source. |
| hw `ari-trunk/apply_ari_trunk_patch.py`, `apply_caller_trunk_patch.py`, `vonage-trunk/setup_vonage.py` | `docker cp` out, patch, mount `provider.py` | `providers/ari/provider.py` | **No.** Source uses `ARI_PJSIP_DEFAULT_TRUNK` and has none of `DOGRAH_ARI_TRUNK`, `DOGRAH_ARI_TRANSFER_TRUNK`, `DOGRAH_ARI_DIAL_PREFIX`, `DOGRAH_ARI_DIAL_FORMAT`, `DOGRAH_ARI_CALLER_TRUNKS`, `Local/` passthrough, or markers `HOPWHISTLE_ARI_TRUNK_V1..V3` / `HOPWHISTLE_ARI_CALLER_TRUNK_V1`. |
| hw `fish-tts` | staged files + 4 mounts | registry, service_factory, check_validity, new `fish_msgpack_shim.py` | Yes (`HOPWHISTLE_FISH_TTS_V1`) |
| hw `transfer-duration-hotfix` | `docker cp` out, patch, mount | `pipecat_engine_callbacks.py`, `pipecat_engine_custom_tools.py` | Yes (`HOPWHISTLE_TRANSFER_DURATION_HOTFIX_V1`) |
| hw `runs-page-speedup` | patch + mount; indexes via `docker exec` | `api/db/{workflow_run,campaign,organization_usage}_client.py` | Yes, done differently (`filters.workflow_run_list_load_options`; migration `c3f9a1d7e4b2`). The indexes have different names, so duplicates are possible if both were applied. |
| hw `direct-transfer`, `inbound-callback`, `{anveo,twilio,vonage}-trunk` installers | marked blocks in Asterisk `pjsip.conf`/`extensions.conf`; DB `UPDATE tools`, `inbound_workflow_id` | none (config/data only) | n/a. These live outside the image and survive image changes. |
| hw `call-quality-bridge`, `export_recordings.py`, `check-dograh-load.sh`, `diagnose-dograh-fractel.sh` | `docker cp` + `docker exec` (read-only) | none | n/a. `diagnose-dograh-fractel.sh` checks patch-only env names and markers, so it would report "not patched" on a source-built image. |

### 10.5 Cutover tooling risk

TOOLING, not applied by this audit.

- `dograh: deploy/hopwhistle/cutover/common.sh:19` hard-codes `NEW_ALEMBIC_HEAD="b7c41d9e52aa"`.
- Current source continues `b7c41d9e52aa → 9d3e1a7c5b20 → a4c8e2f61b93 → c3f9a1d7e4b2`, so the real head is `c3f9a1d7e4b2`.
- Running `2_cutover.sh` from current main would:
  1. migrate the database to `c3f9a1d7e4b2`;
  2. fail its own head check (`2_cutover.sh:121`);
  3. auto-roll back the image while leaving the extra schema changes in place.
- `gen_override.py` also drops the `provider.py` mount but keeps the `DOGRAH_ARI_*` env vars, which source ignores. Trunk prefixes and per-caller-ID trunk routing would silently stop.
- **This must be fixed before any cutover.** It is tracked in the status document.

### 10.6 Override layers, in order of effect

1. `/opt/dograh/docker-compose.override.yaml`: image, mounts, env.
2. Edited base `docker-compose.yaml` (port remaps).
3. `/opt/dograh/.env`.
4. Asterisk config (`/opt/dograh-asterisk/etc/*`, HOPWHISTLE marker blocks).
5. Database rows:
   - `organization_configurations`;
   - `workflow_configurations`;
   - `tools.definition`;
   - `telephony_phone_numbers` (`inbound_workflow_id`, `extra_metadata`);
   - campaign `orchestrator_metadata`.
6. A UI image built from a non-main branch.

---

## 11. Unknowns: need server or database access

1. The running API/UI image tags, digests, and `org.opencontainers.image.revision`; whether the cutover to a fork-built image happened.
2. The full list of bind mounts in `docker-compose.override.yaml`, i.e. which Dograh source files are shadowed by patch copies (in particular `campaign_call_dispatcher.py`, `rate_limiter.py`, `ari/provider.py`).
3. The DB `alembic_version`.
4. The Pipecat commit inside the running image.
5. The commit `dograh-ui:voicestudio` was built from.
6. Which ARI trunk and env names are set. Names only: `ARI_PJSIP_DEFAULT_TRUNK`, `ARI_TRANSFER_DEFAULT_CALLER_ID`, `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION`, `DOGRAH_TRANSFER_DESTINATION`, `DOGRAH_ARI_*`, `DOGRAH_STATE_CID_POLICY`, `CALL_HYGIENE_ENABLED`, `OSS_JWT_SECRET` (presence only).
7. The selected provider/model per active workflow: org `MODEL_CONFIGURATION_V2` mode, workflow overrides, and whether any active workflow is `is_realtime` (and whether it uses `openai_live`).
8. The live workflow definitions: greeting type, per-node `allow_interrupt`, transfer tool `destination_source`, extraction variables, QA node.
9. Which Asterisk HOPWHISTLE blocks are installed (direct-transfer, AI callback, trunks).
10. Whether `push_runs.py` is scheduled; whether `DOGRAH_QUALITY_INGEST_SECRET`, `CALL_QUALITY_ENABLED`, and a transcription key are set in Hoppwhistle; whether any `ExternalAgentBinding` rows exist.
11. Whether `DOGRAH_CALLBACK_BRIDGE` is set in Hoppwhistle, and which numbers carry `metadata.dograhCallback`.
12. Whether `AIVOICE_JWT_SECRET` equals `OSS_JWT_SECRET`. Compare hashes only, never values.
13. Duplicate indexes (`hw_idx_*` vs `idx_workflow_runs_*`).
14. Real-world behavior of the GPT-Live barge-in and greeting gaps (§6.1). This needs a staged call, not just server access.

---

## 12. Read-only server verification checklist (redacted)

**Rules for running these checks:**
- Run them as an operator with read access.
- **Do not** run `env`, `printenv` without a name filter, `docker inspect` without a `--format` filter, or `cat .env`.
- **Do not** restart, recreate, or exec anything that writes.
- Record outputs in the status document, with phone numbers and hosts redacted.
- Paths assume the layout described in §10. Adjust if `docker compose ls` shows otherwise.

### A. Dograh box: images and revisions

```bash
# Compose projects and running containers (names, images, status only)
docker compose ls
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -i dograh

# Image digest and revision labels of the running API/UI containers
for c in dograh-api-1 dograh-ui-1; do
  docker inspect "$c" --format '{{.Name}} image={{.Config.Image}} id={{.Image}}'
  docker inspect "$c" --format '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.created"}}'
done

# Pipecat version and location inside the image (no secrets)
docker exec dograh-api-1 python -c "import importlib.metadata as m, pipecat; print(m.version('pipecat-ai'), pipecat.__file__)"

# Git revision of any source checkout on the host (if present)
git -C /opt/dograh rev-parse HEAD 2>/dev/null; git -C /opt/dograh status --short 2>/dev/null | head
```

### B. Bind mounts and shadowed source files

```bash
# Mounts only: source -> destination (no env)
docker inspect dograh-api-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{println}}{{end}}'

# Override file structure without env values: list image, volumes, and env var NAMES only
grep -nE '^\s*(image|volumes|- /opt|- \./)' /opt/dograh/docker-compose.override.yaml
grep -nE '^\s*-?\s*[A-Z_]+[:=]' /opt/dograh/docker-compose.override.yaml | sed -E 's/([:=]).*/\1 <redacted>/'

# Patch markers present in the running container
docker exec dograh-api-1 sh -c 'grep -rlE "HOPWHISTLE_[A-Z_]+_V[0-9]" /app/api | sort'

# Compare each mounted file with the repo source at a known commit (hash only)
docker exec dograh-api-1 sha256sum /app/api/services/campaign/campaign_call_dispatcher.py \
  /app/api/services/campaign/rate_limiter.py /app/api/services/telephony/providers/ari/provider.py
# then on a workstation: git show <commit>:api/services/... | sha256sum
```

### C. Configuration names, presence only

```bash
# Prints NAME=set|unset for each name; never the value
for n in ARI_PJSIP_DEFAULT_TRUNK ARI_TRANSFER_DEFAULT_CALLER_ID CAMPAIGN_DEFAULT_TRANSFER_DESTINATION \
         DOGRAH_TRANSFER_DESTINATION DOGRAH_ARI_TRUNK DOGRAH_ARI_TRANSFER_TRUNK DOGRAH_ARI_DIAL_PREFIX \
         DOGRAH_ARI_DIAL_FORMAT DOGRAH_ARI_CALLER_TRUNKS DOGRAH_STATE_CID_POLICY CALL_HYGIENE_ENABLED \
         OSS_JWT_SECRET FASTAPI_WORKERS ENABLE_TURN_LOGGING MPS_API_URL LANGFUSE_HOST; do
  docker exec dograh-api-1 sh -c "[ -n \"\${$n+x}\" ] && echo $n=set || echo $n=unset"
done
# Non-secret policy values may be printed explicitly:
docker exec dograh-api-1 sh -c 'echo DOGRAH_STATE_CID_POLICY=${DOGRAH_STATE_CID_POLICY:-<unset>}; echo CALL_HYGIENE_ENABLED=${CALL_HYGIENE_ENABLED:-<unset>}'
```

### D. Database: read-only, no PII

Use `psql` in a read-only transaction: `BEGIN READ ONLY; ... ROLLBACK;`.

```sql
SELECT version_num FROM alembic_version;

-- Org model config: mode and provider/model ids only (no keys).
-- Shape per api/schemas/ai_model_configuration.py: {mode: dograh|byok, dograh:{voice,...}, byok:{mode, pipeline:{llm,tts,stt}, realtime:{realtime,llm}}}
SELECT organization_id,
       value->>'mode' AS mode, value#>>'{byok,mode}' AS byok_mode,
       value#>>'{byok,pipeline,llm,provider}' llm_p, value#>>'{byok,pipeline,llm,model}' llm_m,
       value#>>'{byok,pipeline,stt,provider}' stt_p, value#>>'{byok,pipeline,stt,model}' stt_m,
       value#>>'{byok,pipeline,tts,provider}' tts_p, value#>>'{byok,pipeline,tts,model}' tts_m,
       value#>>'{byok,realtime,realtime,provider}' rt_p, value#>>'{byok,realtime,realtime,model}' rt_m,
       value#>>'{byok,realtime,llm,model}' rt_backend_llm_m
FROM organization_configurations WHERE key = 'MODEL_CONFIGURATION_V2';
-- Never select value as a whole or any api_key path.

-- What runs actually used, last 7 days (runtime_configuration is stamped per run)
SELECT workflow_id,
       initial_context#>>'{runtime_configuration}' AS runtime_cfg,
       count(*)
FROM workflow_runs WHERE created_at > now() - interval '7 days'
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 50;
-- Inspect runtime_cfg for key names before sharing; remove any key material.

-- Per-workflow overrides present (names only)
-- Columns are JSON (not JSONB) per api/db/models.py, hence the casts.
-- Runs pin a definition; workflows.released_definition_id is the published one.
SELECT w.id, w.name, w.status,
       (wd.workflow_configurations::jsonb ? 'model_configuration_v2_override') AS has_v2_override,
       (wd.workflow_configurations::jsonb ? 'model_overrides') AS has_legacy_override,
       wd.workflow_configurations::jsonb #>> '{model_configuration_v2_override,byok,mode}' AS override_mode,
       wd.workflow_configurations->>'turn_stop_strategy' AS turn_stop,
       wd.workflow_configurations->>'buffer_muted_speech' AS buffer_muted
FROM workflows w LEFT JOIN workflow_definitions wd ON wd.id = w.released_definition_id;

-- Transfer tool destination SOURCE type only (not the number). Inspect key names
-- of one definition first (SELECT json_object_keys(definition) ...) if the path is empty.
SELECT id, name, category,
       definition::jsonb #>> '{config,destination_source}' AS dest_source,
       (definition::text LIKE '%PJSIP/hopwhistle%') AS direct_transfer_rewritten
FROM tools;

-- Inbound mapping coverage (counts, no numbers)
SELECT tc.provider, count(*) FILTER (WHERE p.inbound_workflow_id IS NOT NULL) AS with_inbound, count(*)
FROM telephony_phone_numbers p JOIN telephony_configurations tc ON tc.id = p.telephony_configuration_id
GROUP BY 1;

-- Index duplication check
SELECT indexname FROM pg_indexes WHERE tablename = 'workflow_runs' ORDER BY 1;
```

### E. Asterisk: names of installed blocks only

```bash
docker exec dograh-asterisk sh -c 'grep -hn ">>> HOPWHISTLE" /etc/asterisk/pjsip.conf /etc/asterisk/extensions.conf'
docker exec dograh-asterisk asterisk -rx 'pjsip show endpoints' | awk '/Endpoint:/{print $2}'
```

### F. Hoppwhistle side

```bash
cd /opt/hopwhistle && git rev-parse HEAD && git status --short | head
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -i hopwhistle
for n in AIVOICE_URL AIVOICE_JWT_SECRET AIVOICE_COOKIE_DOMAIN AIVOICE_DEFAULT_USER_ID DOGRAH_CALLBACK_BRIDGE \
         DOGRAH_QUALITY_INGEST_SECRET CALL_QUALITY_ENABLED CALL_QUALITY_PROVIDER CALL_QUALITY_MODEL; do
  docker exec hopwhistle-api sh -c "[ -n \"\${$n+x}\" ] && echo $n=set || echo $n=unset"
done
# JWT secret parity without revealing either value: compare sha256 on each box
docker exec hopwhistle-api sh -c 'printf %s "$AIVOICE_JWT_SECRET" | sha256sum | cut -c1-12'
docker exec dograh-api-1   sh -c 'printf %s "$OSS_JWT_SECRET"     | sha256sum | cut -c1-12'
# Is the quality bridge scheduled?
crontab -l 2>/dev/null | grep -n push_runs; systemctl list-timers 2>/dev/null | grep -i -E 'dograh|quality'
```

```sql
-- Hoppwhistle DB (read-only)
SELECT "sourceType", count(*) FROM "ExternalAgentBinding" GROUP BY 1;
SELECT count(*) FROM "CallQualityQuarantine";
SELECT max("createdAt") FROM "ExternalCallRecord";
```

### G. Front door

```bash
nginx -T 2>/dev/null | grep -nE 'server_name|proxy_pass|add_header (X-Frame-Options|Content-Security-Policy)' | grep -i -A2 aivoice
```
