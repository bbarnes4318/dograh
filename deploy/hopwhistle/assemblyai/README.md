# AssemblyAI as the platform STT default: operator runbook

Model `assemblyai-stt-3-7-realtime-agent`, Streaming v3 at
`wss://streaming.assemblyai.com/v3/ws`. Code:
`api/services/configuration/assemblyai.py` (settings),
`api/services/pipecat/assemblyai_stt.py` (the streaming service),
`api/services/pipecat/assemblyai_rollout.py` (what each call uses),
`api/services/pipecat/assemblyai_usage.py` (budgets and metering).

Nothing changes for running calls. Every call decides its STT once, when it
starts.

## Model and audio

- Saved configurations that still name a retired AssemblyAI model
  (`u3-rt-pro`, `u3-rt-pro-beta-1`, `universal-3-5-pro`,
  `universal-streaming-*`) load as `assemblyai-stt-3-7-realtime-agent`, so no
  call runs on them and a re-save stores the new name. `universal-3-6-pro`
  (AssemblyAI's previous flagship) is kept when chosen explicitly.
- Phone calls (8 kHz) are sent as native G.711 mu-law (`encoding=auto`);
  other audio as 16-bit PCM. `encoding` can force either.
- Calls with `language=multi` declare English and Spanish and report the
  spoken language per turn; single-language calls are labelled with it.

## What decides a call's STT

1. The workflow's own STT (model override) is used as it is.
2. Otherwise the organization's STT is used, unless the platform supplied it
   (the org's key is the server's own `DEEPGRAM_API_KEY` /
   `CARTESIA_API_KEY` / ... or HopSTT with no key) **and** the rollout covers
   the call. Then the call uses AssemblyAI on the platform key.
3. Speech-to-speech configs (OpenAI Realtime, Gemini Live, ...) have no STT
   stage and are left alone.

Tenant BYOK keys, Dograh-managed bundles and workflow overrides are never
replaced by the rollout. To switch one of those, use
`scripts/assemblyai_migrate.py` (one target, dry run first, backup file,
exact rollback).

New configurations made in the UI start on AssemblyAI with a blank key, which
means "use the platform key".

## Settings (api container environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASSEMBLYAI_API_KEY` | (none) | Platform key. Required before anything uses AssemblyAI. |
| `ASSEMBLYAI_STT_ROLLOUT` | `off` | `off`, `canary`, or `global`. |
| `ASSEMBLYAI_STT_CANARY_ORG_IDS` / `..._WORKFLOW_IDS` | (none) | Who `canary` covers. |
| `ASSEMBLYAI_STT_EXCLUDED_ORG_IDS` | (none) | Never moved by the rollout, e.g. Holiday Voices orgs. |
| `ASSEMBLYAI_DAILY_BUDGET_USD` | no limit | Optional cap on estimated spend per UTC day on the platform key. |
| `ASSEMBLYAI_TOTAL_BUDGET_USD` | no limit | Optional cap on estimated lifetime spend on the platform key. |
| `ASSEMBLYAI_MAX_CONCURRENT_STREAMS` | no limit | Optional cap on simultaneous platform-key sessions. |
| `ASSEMBLYAI_RESERVE_MINUTES_PER_CALL` | `5` | Reserved per call at start, trued up at the end. |
| `ASSEMBLYAI_FALLBACK_PROVIDER` | (none) | For configs that name AssemblyAI: STT to use when it cannot start. Uses the org's own key for that provider; no key, no fallback. |
| `STT_PLATFORM_DEFAULT_PROVIDER` | `assemblyai` | What new configs start with. `deepgram` rolls that back. |

There are no limits by default: usage and estimated cost are tracked (session
durations times $0.45/hour, +$0.10/hour with Voice Focus; an estimate, not
AssemblyAI's account balance) but never block a call. If an operator sets a
cap and it is reached, new calls the rollout would have moved keep their
original STT; a config that names AssemblyAI uses the fallback, or is refused
with a clear error if none is set. Calls in progress are never cut off.

Tenant (BYOK) AssemblyAI sessions are metered but not limited.

## Rollout

All box changes go through `../cutover/4_set_assemblyai_stt.sh`. It edits
`/opt/dograh/docker-compose.override.yaml` with the cutover's own
line-preserving rewriter (backup kept, `docker compose config` must pass),
asks before recreating the api container if calls look active, and recreates
only `api`. Later `update_api.sh` runs keep these settings.

**Phase 1-2, deploy the code.** `sudo bash ../cutover/update_api.sh` between
campaigns. With no key and the rollout `off`, existing agents are unchanged;
new configurations made in the UI start on AssemblyAI.

**Phase 3, provision the key and smoke test.**

```bash
sudo bash 4_set_assemblyai_stt.sh key      # prompts; the key is never echoed or logged
sudo bash 4_set_assemblyai_stt.sh smoke    # one real session; exit 0 = pass
```

The smoke test opens one real session (1 s of silence, 8 kHz PCM16), checks
that AssemblyAI reports the requested model, terminates cleanly, and prints
latency. Add `--wav file.wav` (mono 16-bit, synthetic or de-identified) to
check a transcript. Superusers can run the same check with
`POST /api/v1/superuser/stt/assemblyai/smoke-test`.

**Phase 4, canary.** See who the platform default would reach, then enable it
for a few orgs or workflows. `rollout canary|global` refuses to run unless the
key is present and the smoke test passes.

```bash
sudo bash 4_set_assemblyai_stt.sh report
sudo bash 4_set_assemblyai_stt.sh rollout canary --orgs 3 --workflows 42
```

Watch `GET /api/v1/superuser/stt/assemblyai/status` (minutes and estimated cost
by org, workflow and product; error rate; fallbacks; streams in use). Each
call's `initial_context.runtime_configuration` records `stt_provider`,
`stt_selection` (`configured` / `rollout` / `fallback`) and the reason.

**Phase 5, global.**

```bash
sudo bash 4_set_assemblyai_stt.sh rollout global
```

Optional caps, if ever wanted: `4_set_assemblyai_stt.sh budget --daily <USD>
--total <USD> --streams <N>` (`unlimited` removes one).

**Rollback.** `sudo bash 4_set_assemblyai_stt.sh rollout off`. The next calls
use exactly what they used before. Orgs switched with
`assemblyai_migrate.py --apply` roll back with
`python -m scripts.assemblyai_migrate --rollback <backup.json> --apply`.

## Turn detection

`turn_detection=assemblyai` (default): AssemblyAI's semantic end of turn ends
the caller's turn, and the user aggregator issues interruptions on
AssemblyAI's speech-start, the same path Deepgram Flux uses. Local VAD does
not start turns, so there is one turn decision per call. `mode`
(`min_latency` / `balanced` / `max_accuracy`) tunes it.

`turn_detection=pipecat`: the workflow's VAD/turn settings decide and each VAD
stop sends `ForceEndpoint`; AssemblyAI only transcribes.

## Vocabulary

The workflow's existing **dictionary** words are sent as keyterms (deduped,
at most 100, each at most 50 characters). A workflow can also opt into presets
with `stt_keyterm_presets` in its configuration: `insurance`, `medicare`,
`real_estate`. Nothing is added by default.
