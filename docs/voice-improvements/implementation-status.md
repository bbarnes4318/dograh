# AI Voice improvement program: implementation status

This page tracks the voice-improvement program across `bbarnes4318/dograh` and
`bbarnes4318/hoppwhistle`.

- Evidence for every finding is in [`baseline.md`](./baseline.md); `§n`
  refers to its sections.
- Last updated: 2026-10-20 (Task 0A).

## Status vocabulary

| Status | Meaning |
|---|---|
| `not started` | No work yet. |
| `in progress` | Work under way. |
| `in review` | A PR is open. |
| `merged` | Merged to the default branch. **Not deployed.** |
| `deployed (unverified)` | Rolled out; live checks not done. |
| `verified live` | The task's live checks passed and are recorded here. |
| `blocked` | Waiting on a dependency or a decision. |

Nothing in this program is deployed or verified live yet. "Merged" means only
that the code is on `main`.

---

## Task 0: architecture and deployment baseline

| Field | Value |
|---|---|
| Status | `merged`: documentation only. Merged in bbarnes4318/dograh#53. Live verification pending. |
| Baseline revisions | dograh `9bf1485`; pipecat submodule `38bf23d`; hoppwhistle `63558a7` |
| Deliverables | `docs/voice-improvements/baseline.md`, this file |
| Pending live verification | Run the 0A collector on the box and fill in the record below. |

### Live baseline record

Fill this in from `deploy/hopwhistle/inventory/collect_inventory.py` (0A).
Never paste secrets, phone numbers or IP addresses. Every row stays `UNKNOWN`
until the collector has been run on the box.

| Item | Collector field | Value | Checked by / date |
|---|---|---|---|
| Running dograh-api image, id, revision label | `dograh.api_image` | UNKNOWN | |
| Running dograh-ui image (`voicestudio`?) | `dograh.ui_image` | UNKNOWN | |
| Pipecat version in the running image | `dograh.pipecat` | UNKNOWN | |
| API bind mounts (destinations) | `dograh.api_mounts` | UNKNOWN | |
| `HOPWHISTLE_*` markers in the container | `dograh.api_patch_markers` | UNKNOWN | |
| DB `alembic_version` (all rows) | `dograh.database.alembic_version` | UNKNOWN | |
| Org AI mode and provider/model ids | `dograh.database.org_model_configuration` | UNKNOWN | |
| Workflows on realtime / `openai_live` | `dograh.database.workflows`, `runs_last_7_days_by_model` | UNKNOWN | |
| Transfer behavior: env policy, forced DID, direct-transfer tools | `dograh.api_environment`, `dograh.database.tools`, `dograh.compose_override` | UNKNOWN | |
| Installed Asterisk `HOPWHISTLE` blocks | `dograh.asterisk` | UNKNOWN | |
| Hoppwhistle commit and dirty files | `hoppwhistle.source_checkout` | UNKNOWN | |
| JWT secret parity (`AIVOICE_JWT_SECRET` vs `OSS_JWT_SECRET`) | `jwt_secret_parity` | UNKNOWN | |
| Call-quality bridge scheduled; binding count | `dograh.schedules`, `hoppwhistle.database` | UNKNOWN | |
| `DOGRAH_CALLBACK_BRIDGE` set; callback numbers | `hoppwhistle.api_environment`, `hoppwhistle.database.callback_numbers` | UNKNOWN | |

---

## Prerequisites 0A–0E

These come before the numbered tasks. They are correctness and safety fixes,
not voice-quality features.

### 0A: Deployment safety and baseline collection

| Field | Value |
|---|---|
| Status | `in review` (this PR). Not deployed. Not run on the box. |
| Depends on | 0 |
| Blocks | every task whose change must reach production |

**Findings addressed in source:**

1. **Hard-coded migration head (P2).** The cutover hard-coded migration head
   `b7c41d9e52aa`, but source is at `c3f9a1d7e4b2`. Running it would have
   migrated, then failed its own check and auto-rolled-back. The head now
   comes from the target image, read statically without starting it.
   (`deploy/hopwhistle/cutover/*`)
2. **Rollback rewrote `alembic_version` (P2).** Rollback and the update
   failure path rewrote `alembic_version` and started the old image against a
   migrated schema. Now:
   - automatic rollback happens only when the database is provably untouched;
   - every other failure holds;
   - recovery is an explicit operator choice through `recover.sh`.
3. **ARI trunk behavior was patch-only (P3).** Trunk, transfer trunk,
   `Local/` passthrough, dial prefix, dial format and per-caller-ID trunks
   existed only in the mounted `provider.py`. They are ported to
   `api/services/telephony/providers/ari/dialing.py` behind explicit
   settings, all off by default.
4. **Forced shared transfer DID was patch-only (P4).** It existed only in a
   hoppwhistle dispatcher copy. It is now explicit configuration:
   `CAMPAIGN_TRANSFER_DESTINATION_POLICY=forced_default`. The default
   `context_first` is unchanged.
5. **The override generator removed every mount (P1).** `gen_override.py`
   removed every api mount without checking it. Mounts are now checked
   against `deploy/hopwhistle/cutover/mount_manifest.json`; unknown mounts or
   behavior block the plan.
6. **No safe way to collect the live baseline.** There is now
   `deploy/hopwhistle/inventory/collect_inventory.py`: allowlisted output,
   read-only transactions, secret parity reported as
   match/mismatch/missing.

**Pending live verification:**

- Run the collector and fill in the live baseline record.
- Dry-run `2_cutover.sh` on the box. Resolve every blocker, especially
  unknown mounts. The baseline notes that 20 or 13 mounts are claimed, and
  the manifest knows 17 targets.
- Cutover in a calling gap, then the parity checklist in
  `deploy/hopwhistle/cutover/README.md`.

**Companion hoppwhistle change (needed after the cutover; not done here):**

- `diagnose-dograh-fractel.sh` checks the patch-era names `DOGRAH_ARI_TRUNK`
  and `HOPWHISTLE_ARI_TRUNK_V1`. On a source-built image it would report "not
  patched".
- The `ari-trunk` and `vonage-trunk` READMEs tell operators to set the
  `DOGRAH_ARI_*` names. After the cutover the names are `ARI_PJSIP_*`,
  `ARI_DIAL_*` and `ARI_CALLER_TRUNKS`.
- The kit `deploy/dograh/patches/campaign_call_dispatcher.py` embeds a
  hard-coded DID in a tracked file. Remove it, or replace it with an
  environment-only value.

### 0B: Credential masking

| Field | Value |
|---|---|
| Status | `not started` |
| Depends on | 0 |

**Findings:**

- `voicemail_detection.api_key` is returned unmasked by the workflow APIs.
  `mask_workflow_configurations` only covers `model_overrides` and the v2
  override. (`api/services/configuration/masking.py:147-215`; baseline §7)
- Whether Langfuse org credentials are masked in the GET response is UNKNOWN.
  (`api/routes/organization.py:1174`)
- Provider keys are stored as plain JSON; application-level encryption is
  absent (§7). Record this; it is not necessarily in 0B's scope.

**Pending live check:** inspect staging API responses for any unmasked key.

### 0C: Realtime speech and interruption correctness

| Field | Value |
|---|---|
| Status | `not started` |
| Depends on | 0, 1 (to measure) |

**Findings (§6.1):**

- **GPT-Live interruptions.** GPT-Live never broadcasts `InterruptionFrame`,
  because `ExternalUserTurnStartStrategy()` defaults to
  `enable_interruptions=False`. `UserStartedSpeakingFrame` only arrives with
  the first transcript delta.
- **GPT-Live greeting.** An audio-file greeting is invisible to GPT-Live,
  which may cause a double greeting. `LLMContextFrame` is forwarded
  downstream.
- **Dropped engine speech.** OpenAI Realtime, Azure, Grok, Gemini and
  Ultravox drop every engine `TTSSpeakFrame` after the opening: transition
  lines, nudges, thinking cues and tool pre-messages are all lost.
- **Mute state.** Those code paths can leave the caller in the `waiting` mute
  state (`pipecat_engine.py:1168-1186`).
- **Output gate.** There is no output gate on Azure, Grok, Gemini or
  Ultravox.

**Source:** `api/services/pipecat/realtime/*`, `run_pipeline.py:216-292`,
`pipecat/src/pipecat/turns/user_start/external_user_turn_start_strategy.py:23`

**Pending live checks:** staged calls on the selected realtime provider only:
barge-in, audio greeting, transitions and tools. The GPT-Live protocol and
model id are unverified.

### 0D: ARI campaign outcomes

| Field | Value |
|---|---|
| Status | `not started` |
| Depends on | 0, 0A (ARI is the live provider) |

**Findings (§3.4):**

- `_process_status_update` is never called for ARI. As a result:
  - there are no busy/no-answer retries;
  - the circuit breaker sees no answer outcomes;
  - caller-ID answer-rate tracking does not run;
  - unanswered runs are never marked completed.
- `retry_on_voicemail` is never triggered: nothing publishes a `voicemail`
  reason.

**Source:** `api/services/telephony/status_processor.py`,
`api/services/telephony/ari_manager.py:393-420`,
`api/services/campaign/campaign_orchestrator.py:185-309`

**Pending live checks:** staged no-answer, busy and voicemail calls on ARI.

### 0E: Transcribed-call grading and delivery

| Field | Value |
|---|---|
| Status | `not started` |
| Depends on | 0 |

**Findings (§9.4):**

- The Hoppwhistle grader needs a transcription key even for Dograh runs,
  which arrive already transcribed. (`hw: apps/api/src/services/call-quality/config.ts:78-89`)
- The run bridge `push_runs.py` has no cron or systemd unit in either repo.
  The collector reports whether one is installed.
- Dograh has no Hoppwhistle delivery path; the bridge reads Dograh Postgres
  directly.

**Pending live checks:**

- On staging: a signed POST is accepted, then graded; an unbound workflow is
  quarantined.
- Record the binding count.

---

## Tasks 1–15

| # | Task | Status | Depends on |
|---|---|---|---|
| 1 | Correct voice latency measurements | not started | 0A |
| 2 | Build conversation replay and evaluation tooling | not started | 1 |
| 3 | Add authoritative conversation state | not started | 2 |
| 4 | Implement speculative response generation | not started | 1, 2, 3, 5 |
| 5 | Improve turn detection, interruptions, and recovery | not started | 0C, 1, 2 |
| 6 | Improve GPT-Live delegation and intelligence | not started | 0C, 2, 3 |
| 7 | Unify voicemail, screener, silence, and closing controls | not started | 0D, 2 |
| 8 | Optimize streaming speech, audio, and greetings | not started | 0C, 1 |
| 9 | Update and benchmark speech/model providers | not started | 0B, 1, 2 |
| 10 | Strengthen transfers and callback execution | not started | 0A, 0D, 3 |
| 11 | Complete Hoppwhistle handoff integration | not started | 0E, 10 |
| 12 | Connect voice performance to business outcomes | not started | 0E, 1, 11 |
| 13 | Separate customer workspaces and identity | not started | 0B, 11 |
| 14 | Validate capacity, failure recovery, and release readiness | not started | 0A, 1, 2 |
| 15 | Experimental PersonaPlex evaluation (optional) | not started | 1, 2 |

### 1. Correct voice latency measurements

**Findings (§8):**

- STT and TTS TTFB are not persisted; only LLM TTFB is
  (`realtime_feedback_observer.py:227-253`).
- `VoiceLatencyTracker` timings for OpenAI Realtime and GPT-Live are written
  to logs only (`voice_latency.py`).
- The public usage response drops latency, TTFB and `num_turns`
  (`run_usage_response.py:4-13`).
- The STT usage bucket is never filled (`pipeline_metrics_aggregator.py:34-50`).
- `turn_context.py` is a process-global singleton shared by concurrent calls
  when `ENABLE_TURN_LOGGING` is on.

**Pending live check:** compare measured p50/p95 against recordings on staged
calls.

### 2. Build conversation replay and evaluation tooling

**Findings:**

- No conversation replay or eval harness exists for the voice pipeline.
- The prerequisites already have mocked-provider tests to build on
  (`api/tests/test_openai_live_service.py`, `api/tests/test_cutover_*`).
- `evals/` exists at the repo root.

**Pending live check:** none; offline. Each later task's acceptance gate
should use it.

### 3. Add authoritative conversation state

**Findings (§6.2):**

- Qualification is node extraction plus end-node `USER_QUALIFIED`.
- Transfer is gated only by the model's tool call; the app enforces only the
  hygiene block.
- The rules live in workflow prompts stored in the database.

**Source:** `pipecat_engine.py:476-478, 601-726, 977-1035`,
`pipecat_engine_variable_extractor.py`

**Constraint:** do not alter campaign qualification requirements without
approval.

**Pending live check:** staged calls with qualifying and non-qualifying
scripts.

### 4. Implement speculative response generation

**Findings:** none in source yet. It depends on measured latency (1), replay
(2), authoritative state (3) and turn detection (5).

### 5. Improve turn detection, interruptions, and recovery

**Findings (§5, §6.2):**

- VAD `stop_secs=0.2` is hard-coded (`run_pipeline.py:238, 944`).
- Turn start/stop strategies, mute strategies, per-node `allow_interrupt` and
  the output gate interact. Frames queued straight to `transport.output()`
  bypass the gate.
- `MutedSpeechBufferProcessor` relies on the private `_user_is_muted`.
- Caller muting and buffered-speech replay (`muted_speech_buffer.py`).

**Pending live check:** scripted barge-in at each node type; the caller
speaks first, during the greeting, and while muted.

### 6. Improve GPT-Live delegation and intelligence

**Findings (§6.1):**

- The frontend/backend split and the backend model default (`gpt-4.1`).
- Transition deferral is capped at 6 s.
- The protocol is hand-vendored "from pipecat-ai 1.12"
  (`openai_live/events.py:9-13`) and will drift from the real API.

**Correctness defects are in 0C.**

**Pending live check:** staged GPT-Live calls once 0C is done.

### 7. Unify voicemail, screener, silence, and closing controls

**Findings:**

- Call hygiene: `MachineAnswerGuard` and `AssistantTurnGuard`, chained
  pipelines only, deployment-wide switch `CALL_HYGIENE_ENABLED`.
- `VoicemailDetector` is disabled for realtime.
- Idle nudges.
- Twilio/Vonage carrier AMD results are stored and never read.
- `retry_on_voicemail` is dead (see 0D).

**Source:** `call_hygiene.py`, `run_pipeline.py:1150-1189`,
`schemas/workflow_configurations.py:91-174`. Day-one review tooling:
`deploy/hopwhistle/cutover/day_one_review.*`.

**Pending live check:** day-one review after the guards are enabled.

### 8. Optimize streaming speech, audio, and greetings

**Findings:**

- Greeting ownership has three modes: audio file, static text, LLM-generated
  (`pipecat_engine.py:908-965`).
- TTS text filter, ambient noise, thinking-cue phrasing.
- The Fish shim exists because the image omits the pipecat `fish` extra
  (`api/Dockerfile:38-43`).
- Where OpenAI TTS 24 kHz is resampled for 8 kHz telephony is UNKNOWN.

**Pending live check:** listening review of anonymized staged recordings.

### 9. Update and benchmark speech/model providers

**Findings (§7):**

- Model ids are free text, with no allowlist (`registry.py`).
- `check_validity.py` always passes several providers without a live check.
- STT keyterms (`dictionary`).
- Pipecat fork pinned at `38bf23d`; its version string probably reports
  `0.0.0-dev`.

**Constraint:** do not change selected providers or production defaults
without approval.

**Pending live check:** the collector records the selected ids; WER and
latency on anonymized fixtures.

### 10. Strengthen transfers and callback execution

**Findings (§3.4, §4):**

- **Destination authority.** Source uses lead → campaign → default; the box
  may use a forced shared DID. 0A makes this explicit configuration;
  verifying which one is live remains.
- **Transfer-leg caller ID.** It always comes from
  `ARI_TRANSFER_DEFAULT_CALLER_ID`; the tool never passes `caller_id`.
- **Failure handling.** Transfer timeout and failure paths need hardening.
- **Tool timeouts.** HTTP/MCP tools default to a 5 s timeout.
- **Inbound callback.** The Dograh side selects by `inbound_workflow_id`.
  The Hoppwhistle side is `DOGRAH_CALLBACK_BRIDGE`; its own README says it
  is untested live.

**Source:** `pipecat_engine_custom_tools.py:833-1316`,
`ari/provider.py:429-547`, `ari/dialing.py`, `ari_manager.py:1042-1156`

**Pending live check:** staged transfer to a test DID covering:

- destination answers;
- two-way audio;
- the max-duration timer is disarmed;
- a callback to an AI caller ID reaches the assigned agent.

This touches the hoppwhistle FreeSWITCH protected area, so the change-control
gate applies.

### 11. Complete Hoppwhistle handoff integration

**Findings (§9.3):**

- **Transferred-call routing.** Transferred calls route through FreeSWITCH
  `inbound-ai-transfer` → `/freeswitch/lookup?via=dograh` like a normal
  inbound call. No AI context reaches the agent.
- **Direct-transfer kit.** The kit rewrites `tools.definition`; whether it is
  applied is UNKNOWN (the collector reports it).
- **Grading bindings.** Grading attribution needs hand-made
  `ExternalAgentBinding` rows.

**Protected files involved:** `did-routes.ts`, `apps/freeswitch/**`.

**Pending live check:** a transfer lands with the expected caller ID and
routing on staging.

### 12. Connect voice performance to business outcomes

**Findings (§8):**

- QA node, reports and cohorts by `usage_info.latency.p95_seconds`.
- Hoppwhistle grading is aggregated separately for AI.
- `cost_info` has no writers (legacy).

**Pending live check:** a cohort report joining latency to transfer rate on
real (anonymized) data.

### 13. Separate customer workspaces and identity

**Findings (§9.1-§9.2):**

- **One shared identity.** Every Hoppwhistle user is signed in as Dograh user
  `AIVOICE_DEFAULT_USER_ID` (default `1`). Per-user provisioning is
  `TODO(phase-3)`.
- **Upgrade gating.** The `VOICE_AGENTS` upgrade does not unlock the
  staff-only API.
- **SSO from netenroll.** SSO breaks from `agents.netenroll.com`
  (cookie domain).
- **Isolation.** Dograh has no agency isolation.

**Protected files involved:** `aivoice.ts`, `aivoice-jwt.ts`. Requires
explicit authorization.

**Pending live check:** JWT secret parity (collector); an SSO login per
agency on staging.

### 14. Validate capacity, failure recovery, and release readiness

**Findings:**

- **Release path.** Converge on source-built images. The 0A cutover is the
  first step; the UI image is still built from `hopwhistle/customizations`.
- **Recording buffers.** In-memory buffers are capped at 100 MB each
  (`in_memory_buffers.py`).
- **Shared process.** ari_manager, the orchestrator, arq and uvicorn share
  one container.
- **Concurrency.** Limits come from `DEFAULT_ORG_CONCURRENCY_LIMIT`.
- **Duplicate indexes.** `hw_idx_*` and `idx_workflow_runs_*` may both exist
  (the collector reports them).

**Constraint:** no paid load tests as part of these prompts.

**Pending live checks:**

- Capacity is measured on staging only.
- Restore-from-backup is rehearsed on a scratch database
  (`deploy/hopwhistle/cutover/README.md`).

### 15. Experimental PersonaPlex evaluation (optional)

**Findings:** none in source. Optional; run offline against task 2's replay
fixtures before any live exposure.

---

## Cross-repository changes and rollout order

| Change | dograh | hoppwhistle | Order |
|---|---|---|---|
| 0A cutover (ARI settings, transfer policy, planner) | this PR | companion script/README updates listed under 0A | dograh image first, then the cutover on the box, then the hoppwhistle docs/scripts |
| 0E grading of transcribed runs | optional latency fields | ingest gate, bridge schedule | hoppwhistle first if the payload is additive |
| 13 workspaces / SSO | per-org users | protected `aivoice.ts`, `aivoice-jwt.ts` | requires explicit authorization |

## Protected areas to respect

- **Hoppwhistle.** Protected by `hw: .agents/AGENTS.md` and
  `hw: docs/saas/PROTECTED_SYSTEM_INVARIANTS.md`:
  - `apps/freeswitch/**`, `apps/kamailio/**`, `apps/rtpengine/**`
  - `packages/routing-dsl/**`
  - `routes/agent-phone.ts`, `did-routes.ts`, `aivoice.ts`
  - `lib/aivoice-jwt.ts`
  - the recording, carrier and routing services
  - the telephony change-control gate: staging, config diff, test call,
    two-way audio, recording, rollback, approval
- **Dograh.** No protected-file list exists in this repository.
