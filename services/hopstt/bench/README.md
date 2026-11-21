# HopSTT accuracy benchmark and capacity test

> **No results exist yet.** Nothing in this directory reports a measured number.
> Accuracy, latency and capacity are only meaningful once this has been run on
> the real GPU hardware against the private, human-verified call corpus. The
> development container has no GPU and no corpus. The tests here use a protocol
> fake and synthetic tones.

## Dependencies (suggested `bench` extra)

```toml
bench = [
  "numpy>=1.26,<3",
  "httpx>=0.27,<1",
  "websockets>=13",
]
```

That is all. WER/CER, the normalizer, the bootstrap and the G.711 encoders are
self-contained (no jiwer, scipy or audioop). The `ffmpeg` binary is needed only
to decode non-WAV audio or to resample, which matters for `dataset prepare` on
MP3/stereo-16 kHz sources. Tests: `pytest` (no pytest-asyncio).

## Data rules (enforced)

- The corpus (`--corpus` / `HOPSTT_EVAL_CORPUS`) and all outputs must be
  **outside any git working tree**. Every tool refuses paths inside one unless
  you pass `--allow-in-repo`, which is for synthetic fixtures only.
- Outputs default to `$HOPSTT_BENCH_OUT` or `~/hopstt-bench-results/<tool>-<ts>`.
  Directories are created `0700` and text-bearing files `0600`.
- Run directories (`items.jsonl`) contain hypothesis text and are as private as
  the corpus. `report` and `capacity` outputs contain **anonymized aggregates
  only** (opaque ids and numbers, no transcript text), unless you pass
  `report --include-text`, which adds `items_text.private.csv` for local review.
- The tools talk only to HopSTT endpoints. Cartesia output is **imported from
  the manifest** (`baseline.cartesia`) as a baseline, never called, and never
  used as ground truth.
- See [DATASET.md](DATASET.md) for the manifest format, preparation, sampling
  plan, reviewer procedure and access control.

## Tools

All tools are run from `services/hopstt/` as `python -m bench.<tool> --help`.
They share `--url` (`$HOPSTT_URL`), `--api-key` (`$HOPSTT_API_KEY`), `--ws-url`,
`--corpus`, `--manifest`, `--language`, `--tags`, `--limit` and `--out`. Only
verified items are used unless you pass `--all-items`.

| tool | what it does |
|---|---|
| `bench.dataset prepare / apply-review / validate` | build the draft corpus from an export, merge reviewer corrections, check schema + distribution vs targets |
| `bench.run_batch` | every item → `POST /v1/transcriptions` (or `--mode jobs`: `POST /v1/jobs` + poll), per model |
| `bench.run_stream` | every item streamed over `/v1/stt/stream` in real time; latency, stability, failures; `--chunk-ms` sweep; `--kill-fraction` recovery test |
| `bench.report` | score all runs + imported baselines, paired comparisons, acceptance gate → `results.json`, `summary.md`, CSVs |
| `bench.capacity` | live ramp / batch throughput / mixed load; stops at the first threshold breach |
| `bench.fake_server` | protocol **fake** for dry runs of the tooling (no ASR; scripted text) |

```bash
export HOPSTT_URL=http://10.0.0.5:8788 HOPSTT_API_KEY=... HOPSTT_EVAL_CORPUS=/secure/hopstt-corpus
export HOPSTT_BENCH_OUT=/secure/hopstt-runs

python -m bench.dataset validate --check-audio
python -m bench.run_batch  --models parakeet-tdt-0.6b-v3,nemotron-3.5-streaming --out $HOPSTT_BENCH_OUT/batch
python -m bench.run_stream --models nemotron-3.5-streaming --chunk-ms 80,160,320,560,1120 \
       --out $HOPSTT_BENCH_OUT/stream
python -m bench.run_stream --models nemotron-3.5-streaming --chunk-ms 320 --kill-fraction 0.5 \
       --out $HOPSTT_BENCH_OUT/recovery
python -m bench.report --runs $HOPSTT_BENCH_OUT/batch $HOPSTT_BENCH_OUT/stream \
       --baseline cartesia --out $HOPSTT_BENCH_OUT/report
python -m bench.capacity --mode live  --model nemotron-3.5-streaming --chunk-ms 320 --out $HOPSTT_BENCH_OUT/cap
python -m bench.capacity --mode batch --batch-jobs 300 --batch-concurrency 4 --out $HOPSTT_BENCH_OUT/cap-batch
python -m bench.capacity --mode mixed --model nemotron-3.5-streaming --out $HOPSTT_BENCH_OUT/cap-mixed
```

### run_stream options that change what is measured

- `--speed 1.0` (default) paces audio in real time, and `0` sends as fast as
  possible. Latency numbers are valid only at 1.0. `0` is for quick accuracy
  passes and RTF.
- `--chunk-ms` is the server model chunk (`config.chunk_ms`). `--frame-ms` is
  the size of the client's WebSocket frames (default 20, like Dograh; `0`
  means the same as the chunk).
- `--encoding pcm_s16le|mulaw|alaw` and `--sample-rate 8000|16000`.
- `--finalize-at end` (default) sends `finalize` after the last audio of the
  item. `speech_end` sends it at `speech_end + --vad-stop-ms` (default 200 ms,
  Dograh's Silero `stop_secs`), which simulates a VAD stop. It needs
  `speech_end` in the manifest.
- `--kill-fraction F` / `--kill-at-s S` (with `--kill-every N`) run the
  recovery test. The client aborts the TCP connection at that audio position
  and reconnects following PROTOCOL §2.5: `generation + 1`, the same
  `session_id`, `stream_offset_ms` set to the last committed final `end_ms`,
  replay of the uncommitted audio (capped at 30 s), stale-generation messages
  dropped, and already-committed finals dropped. Reported: recovery success
  rate, recovery time (abort → new `ready` + replay), duplicate-final rate and
  stale messages dropped.

## Metrics: definitions and what they include

### Normalization (applied to references, hypotheses and field values)

This is `bench/normalize.py`; its docstring lists every step. In summary:
lower case; punctuation stripped; diacritics stripped (`sí` = `si`); EN
contractions expanded; hesitations (uh/um/eh) removed while back-channels are
kept; spoken numbers turned into digits (EN, and basic ES cardinals such as
`veinticinco mil`); money (`$25,000` = `twenty five thousand dollars` =
`25000 dollar`; `$25.50` = `25 dollar 50 cent`); numeric dates turned into
`month day year` (ES: `day de mes de year`); ordinals (`3rd` = `third` = `3`);
upper-case US state codes and Spanish state names turned into the English state
name; `oh` → 0 between digits; and **adjacent digit tokens merged** so phone
numbers and dates compare equal however they are grouped or spoken. It is
conservative on purpose: anything else counts as an error. Two known side
effects: a number/number boundary error counts as one token, and `sí`/`si` are
merged.

### Accuracy (bench/metrics.py)

| metric | definition |
|---|---|
| WER | Levenshtein over normalized words; S/D/I reported; **micro-averaged** (Σ errors / Σ reference words) overall, per language, per tag. Items with an empty reference are excluded (they go to hallucination). A failed request is scored as an empty hypothesis (all deletions). |
| CER | Same over normalized characters (spaces included). |
| Critical-field accuracy | Per field type: correct if the normalized value appears in the hypothesis (contiguously). With a `span`, only in the hypothesis words aligned to that reference span. Polarity fields are scored by polarity. Numeric accuracy covers `age`, `dob`, `phone`, `coverage_amount`, `premium_amount`. |
| Negative-flip rate | Over polarity fields (`yes_no`, `do_not_contact`, `agent_transfer_consent`, `correction`) whose expected polarity is **negative**: the fraction where the hypothesis is not negative. Split into negative→affirmative and negative→unclear (the negation was lost). The other direction (affirmative→negative) is reported separately. Cues: EN no/nope/not/never/don't/"not interested"/"stop calling"…, ES no/nunca/jamás/nada/ninguno/"deje de llamar"…; "no problem"/"cómo no" count as affirmative. |
| Hallucination | On empty-reference (silence/noise/hold-music) items: words emitted per item, and the % of items with any output. |
| Bootstrap CI | 95 % item-level (cluster) bootstrap, 2000 resamples, fixed `--seed`. |
| Paired comparison | candidate − baseline on the **same items**, bootstrapped by resampling items jointly. Used for every gate decision, so a winner is never declared on noise. |

### Streaming latency (client clock, `bench/client.py:StreamRecord`)

| metric | from → to | includes | excludes |
|---|---|---|---|
| `ttfp_ms` (time to first useful partial) | first frame containing speech (`speech_start`, default 0) sent → first non-empty transcript | network RTT, **server chunk buffering (≥ chunk_ms)**, queue wait, inference | VAD |
| `finalize_to_final_ms` (end-of-speech → final) | `finalize` sent → final received | network, server right-context flush/padding, queue wait, inference | VAD delay (finalize is explicit) |
| `speech_end_to_final_ms` | last speech frame sent → final | the above plus the simulated VAD stop delay and remaining pacing | |
| `first/last_partial_to_final_ms` | first/last interim of the utterance → its final | | |
| `revision_rate` | fraction of successive interims that changed a word other than the previous interim's last word (a growing last word is not a revision) | | |
| `partial_edit_mean` | mean token edit distance between successive interims | | |
| `wall_rtf` | (final − first audio sent) / audio duration | pacing, so ≈1 at speed 1.0; meaningful with `--speed 0` | |
| `connect_ms` | connect start → `ready` | TCP/TLS/WS upgrade, auth, admission, model session setup | |

Percentiles are p50/p95/p99, with `n` reported (p99 needs ≥ 100 samples to
mean anything). Server-reported components: protocol v1 transcripts carry no
timing breakdown. If the server adds a `timings` object to finals it is
recorded per item (`server_timings`), and batch runs record the server's
`decode_ms`/`queue_ms`/`inference_ms` next to client wall time. With
`--chunk-ms 320`, TTFP cannot be below about 320 ms, because a whole chunk must
arrive first. The 250 ms first-partial target is therefore reachable only with
chunks ≤ 160 ms. The sweep shows this trade-off.

## Acceptance criteria (gate, `metrics.acceptance_gate`)

A candidate is compared with the baseline (default: the imported Cartesia
transcripts) on the same verified items. The verdict is
**PASS / FAIL / INSUFFICIENT_DATA**, with reasons:

| check | PASS when | FAIL when |
|---|---|---|
| sample size | ≥ 200 verified paired items **and** ≥ 50 Spanish | otherwise → INSUFFICIENT_DATA (overrides everything) |
| WER ≤ baseline (CI-aware) | diff ≤ 0 and 95 % CI upper bound ≤ margin (default 0.01 abs) | CI lower bound > 0 (significantly worse) or diff > margin; also when any language's WER is significantly worse |
| critical-field accuracy ≥ baseline | shortfall ≤ 0 and its CI upper bound ≤ margin (0.01) | significantly lower or shortfall > margin |
| negative-flip rate ≤ baseline | diff ≤ 0 and CI upper bound ≤ margin (0.01) | significantly higher or diff > margin |
| hallucination ≤ baseline | words/item and any-output rate both ≤ baseline (≥ 10 silence items) | either higher |
| latency (stream candidates) | `first_partial` p95 < **250 ms** and `finalize→final` p95 < **300 ms** | either at or above target. Batch candidates: not applicable |

If no check fails but any check is inside the noise band, the verdict is
INSUFFICIENT_DATA ("collect more data"), not PASS. Margins, targets and the
latency statistic are flags on `bench.report`. Capacity is accepted separately:
the measured capacity (the last ramp step passing finalize→final p95 ≤ 300 ms,
error rate ≤ 1 % and WER delta ≤ 0.02) must cover the target number of
concurrent calls with headroom.

## Capacity test (bench/capacity.py)

- **live**: first a solo pass (each pool file streamed once, alone) gives the
  reference hypotheses. Then for each step (default `1,5,10,20,50,100,200`,
  `--step-duration 60` s, starts staggered by `--stagger-ms`), N streams loop
  over the pool at real-time pace. Per step it records finalize→final and TTFP
  p50/p95/p99; error codes (`capacity`, `overloaded`, ...); dropped sockets;
  missing finals; the error rate; the WER delta against the solo pass (against
  verified references when available, otherwise divergence from the solo
  hypothesis); the client's own CPU use; and `/v1/capacity` + `/metrics`
  snapshots before, at mid-step and after, with counter deltas (GPU util/VRAM
  if the server exports them). It stops at the first step that breaches
  `--max-p95-final-ms 300`, `--max-error-rate 0.01` or `--max-wer-delta 0.02`.
  The **measured capacity is the last passing step**. Reconnects are off by
  default (`--max-reconnects 0`), so every failure counts.
- **batch**: `--batch-jobs` jobs at `--batch-concurrency`, sync or jobs API.
  Reports throughput (audio-s/s), RTF (server inference / audio duration),
  server queue time, client wall time and 429s.
- **mixed**: the live ramp alone, then the same ramp with a batch backlog
  running. Reports the per-step p95 delta, so you can check that batch work
  does not degrade live latency.
- Run it from **a separate machine on the same network as Dograh** (not on the
  GPU host), so network latency is realistic and client CPU does not steal from
  the server. One asyncio process drives a few hundred streams. If
  `client_cpu_util` approaches 1.0, the client is the bottleneck: raise
  `--frame-ms` or shard the load across processes or machines and add up the
  results.

## GPU benchmark runbook

Run these steps in this order and stop when a step fails. Record the GPU model,
driver, image digest, model ids and `HOPSTT_*` settings with every result.

1. **Corpus:** `bench.dataset validate --check-audio --strict` passes (≥ 200
   verified, ~70/30, field and silence minimums).
2. **Batch accuracy:** run `bench.run_batch` for every recorded-call candidate
   (parakeet-tdt-0.6b-v3, Nemotron 3.5 offline, challengers), then
   `bench.report`. The gate decides the recorded-call engine.
3. **Streaming accuracy per chunk size:** run `bench.run_stream --chunk-ms
   80,160,320,560,1120` for each streaming candidate, with `--speed 1`,
   concurrency 1 and `--frame-ms 20`. Read `sweep.md` (accuracy against
   TTFP/finalize latency), pick the chunk, and run `bench.report` (the gate
   includes latency).
4. **Recovery:** `bench.run_stream --kill-fraction 0.5` at the chosen chunk
   size. Expect a recovery success rate near 100 % and a duplicate-final rate
   of 0.
5. **Capacity ramp:** `bench.capacity --mode live` at the chosen chunk size,
   from the Dograh-side machine. Watch VRAM and GPU utilisation in the
   snapshots.
6. **Mixed:** `bench.capacity --mode mixed` with the production batch settings
   (`HOPSTT_ROLE=both`, if that is the plan). The live p95 must not move beyond
   the threshold.
7. Publish `summary.md` files only (aggregates). Keep run directories private.

## Dry run without a GPU

```bash
python -m bench.fake_server --port 8790 --transcripts scripted.json &   # FAKE, scripted text
python -m bench.run_stream --url http://127.0.0.1:8790 --ws-url ws://127.0.0.1:8791/v1/stt/stream \
       --api-key test-key --corpus /tmp/synthetic-corpus --models fake-streaming --speed 0
```

## Tests

```bash
cd services/hopstt && python -m pytest -q tests/test_bench_*.py
```

These cover the normalizer (EN/ES numbers, money, states, dates), WER/CER,
critical fields, negative flips, hallucination, bootstrap determinism, the gate
verdicts, manifest validation, prepare/apply-review, the repo-path guard, the
codecs, and runner, recovery, report and capacity logic against the in-process
fake. All audio is synthetic (tones, silence, noise).

## Limitations

- The normalizer is rule-based and covers common EN/ES forms. Decimal speech
  ("two point five"), Spanish ordinals beyond `primero`, number words used as
  names, and homophones are not unified. Extend it, with tests, if review finds
  systematic false errors.
- Polarity detection is lexical. "I'm not sure" counts as negative, and double
  negatives are not resolved. Review flips by hand (`--include-text`) before
  acting on them.
- Without reference speech timings, TTFP is measured from the first audio frame
  (`speech_start` defaults to 0). Add `speech_start`/`speech_end` to the
  manifest for precise numbers.
- Recovery is tested by a client-side abort only. Server-side crashes need a
  manual `docker kill` during a run.
- Latency is measured on the client clock and includes the client's own
  scheduling. Keep the client machine lightly loaded.
