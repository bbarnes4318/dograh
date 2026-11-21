# HopSTT controlled migration and rollback

Nothing switches automatically. Every stage below needs a named approver and has an
exit gate. Paid STT stays configured, though unused, until Stage 10, so that rollback
is only a configuration change.

| Stage | What | Gate to leave the stage | Rollback |
|---|---|---|---|
| 1 | Local development and tests. Merge both PRs. `HOPSTT_ENABLED=false` (Dograh) and `HOPSTT_URL` unset (Hoppwhistle) keep production unchanged | Local CI passes in both repos | Revert the PR |
| 2 | **Approval needed:** rent an isolated GPU (us-east, hourly). Deploy `docker-compose.gpu.yaml` (live + batch) on a private network with a bearer key | `/readyz` returns 200 on both roles; smoke stream and smoke batch succeed | Stop the instance |
| 3 | Benchmark authorised recordings (`bench/`, private corpus of ≥200 verified segments, ~70 % EN / 30 % ES). Run batch accuracy, then stream accuracy per chunk size, then capacity ramp, then mixed live+batch | Acceptance gate PASS against Cartesia on the same references: WER, critical fields, **negative-flip rate**, hallucinations, latency. Chunk size picked from the results | Choose another model or chunk size, or stop |
| 4 | Simulated live conversations: Dograh staging, or a test org with `HOPSTT_ENABLED=true` on a test runtime, with scripted callers (EN+ES, voicemail greetings, interruptions, short yes/no answers) | No fatal STT errors; transfers and voicemail decisions match the Cartesia baseline; finalize→final p95 ≤ 300 ms | Turn the flag off |
| 5 | Shadow test (authorised calls only): run the HopSTT client alongside the real provider and record only aggregate metrics and verified-sample WER. **The live conversation is never affected.** Uses the Dograh shadow tap (`HOPSTT_SHADOW_ENABLED=true`, `HOPSTT_SHADOW_WORKFLOW_IDS=<ids>`), which passes frames through untouched and feeds a secondary HopSTT session. | Agreement with the production provider within the accuracy gate | Remove the tap |
| 6 | Migrate **one designated test agent**: `python -m api.scripts.hopstt_migrate --workflow <id> --apply`. The script saves the prior STT JSON | One week with no STT-attributable failed calls; transfer, voicemail and qualification rates within normal variance | `hopstt_migrate --workflow <id> --rollback` restores the saved STT JSON |
| 7 | Small group of English and Spanish agents | Same as Stage 6, per language | Per-workflow rollback |
| 8 | Expand by organisation (`--org <id>`), each one gated on capacity (`hopstt_live_capacity_available` > 30 % at peak) | Measured capacity with ≥ 30 % headroom; second live instance online | Per-org rollback |
| 9 | Recorded calls: set `HOPSTT_URL` in Hoppwhistle. HopSTT becomes the default engine; paid engines are excluded unless `TRANSCRIPTION_PAID_FALLBACK_ENABLED=true`. Saved transcripts are **not** re-transcribed | Batch WER gate passed; analyzer backlog stays bounded | Unset `HOPSTT_URL` and redeploy: the Cartesia path returns exactly as before |
| 10 | Disable routine paid STT: remove Cartesia keys from routine paths and keep them only in the break-glass runbook | 30 days of stable operation | Restore the key from the secrets store |

## Rollback principles
- **Dograh:** the Cartesia provider and every saved configuration remain valid in the
  registry. Rollback restores a workflow's or org's STT block, or flips `HOPSTT_ENABLED`
  off. With the flag off, configurations that select HopSTT fail validation clearly,
  so roll configurations back *before* turning the flag off.
- **Hoppwhistle:** with `HOPSTT_URL` unset, engine selection is exactly the
  pre-HopSTT behaviour (covered by tests). Transcripts in `transcriptions` from
  either engine stay valid.
- **HopSTT service:** images are tagged and model revisions pinned. To roll back,
  redeploy the previous tag; `HOPSTT_*_REVISION` keeps the model weights identical.
  The service holds no persistent state (jobs are in memory), so there is nothing
  to back up beyond the model cache, and the model cache can be re-downloaded.
- **Deploys during business hours:** drain first. SIGTERM marks the worker not-ready
  and waits up to `HOPSTT_DRAIN_TIMEOUT_S` (600 s, the same as the FreeSWITCH deploy
  rule) for streams to end. Stop live instances one at a time so clients fail over to
  the other.
