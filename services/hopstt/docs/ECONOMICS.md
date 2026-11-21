# HopSTT economics

Status 2026-11-21. **No billing or usage data was available to this analysis.** I
had no access to the Cartesia dashboard, and no production database query could be
run. Every volume below is a scenario, not a measurement. To turn this into a
decision, fill in the two inputs in §4.

## 1. What Cartesia costs us per minute today

Cartesia bills in credits. Converted from public sources (confirm against your plan
and invoices):

| Use | Model | Rate | $/audio-minute |
|---|---|---|---|
| Live streaming (Dograh) | ink-whisper | 1 credit/s ≈ $0.13/h on Scale (LiveKit lists $0.0023 Scale / $0.0030 Build) | **$0.0022–0.0030** |
| Live streaming | ink-2 | 3 credits/s ≈ $0.39/h on Scale | $0.0065 |
| Recorded calls (Hoppwhistle) | ink-whisper batch | 1 credit / 2 s ≈ $0.07/h | **$0.0012 per channel-minute**. Hoppwhistle sends each stereo leg separately, so ≈ **$0.0023 per call-minute** |

**Zero API cost is not zero operating cost.** Cartesia ink-whisper is already cheap,
so self-hosting saves money only above a meaningful monthly volume.

## 2. HopSTT fixed monthly cost (scenarios)

| Item | Low | High | Notes |
|---|---|---|---|
| Live: 2× L4 in us-east (N+1) | $580 (US dedicated ≈ $290 each, location to be confirmed) | $1,160 (AWS g6.xlarge on demand) | A 1-year commitment or a reserved instance lowers the high case by roughly 30–40 % |
| Batch: 1× Hetzner GEX44 | $211 + ≈ $105 one-off setup | — | CPU workers are an alternative at low volume |
| Monitoring / storage / egress | ≈ $20–40 | | Reuses the existing Prometheus/Grafana. Private traffic between Ashburn and US-East is small: 8 kHz PCM16 is 0.96 MB/min |
| Engineering / on-call time | not costed | | Real but separate. It is the main hidden cost |

Idle capacity is the main cost driver. GPUs are paid for 24/7, while calls are
concentrated in business hours. The second live GPU (redundancy) is idle by design.

## 3. Break-even

| Workload | HopSTT fixed $/mo | Cartesia $/min | Break-even volume |
|---|---|---|---|
| Live (2× L4) | $620–1,180 | $0.0022 (Scale) | **≈ 290k–545k live minutes/month** |
| Live (2× L4) | $620–1,180 | $0.0030 (Build) | ≈ 205k–395k live minutes/month |
| Batch (GEX44) | $231 | $0.0012 per channel-minute | **≈ 200k channel-minutes ≈ 100k stereo call-minutes/month** |
| Combined (1 shared L4 + 1 standby L4, `role=both`) | $620–1,180 | blended | between the two rows above, and needs measured headroom |

Cost per minute at different volumes (monthly):

| Minutes/month | Cartesia live (Scale) | HopSTT live $/min | Cartesia batch (per call-min) | HopSTT batch $/call-min |
|---|---|---|---|---|
| 25,000 | $54 | $0.025–0.047 | $58 | $0.0092 |
| 100,000 | $217 | $0.006–0.012 | $233 | $0.0023 |
| 250,000 | $542 | $0.0025–0.0047 | $583 | $0.0009 |
| 500,000 | $1,083 | $0.0012–0.0024 | $1,167 | $0.0005 |
| 1,000,000 | $2,167 | $0.0006–0.0012 | $2,333 | $0.0002 |
| 2,000,000 | $4,333 | $0.0003–0.0006* | $4,667 | $0.0001 |

\* At around 1M or more live minutes a month, peak concurrency (roughly 200+ streams
in business hours) may need more than two L4s. Cost rises in steps of one GPU. The
capacity test sets those steps.

## 4. Inputs needed for a final decision
1. **Monthly Cartesia STT minutes and dollars**, split into streaming vs batch, from
   the Cartesia dashboard or invoices.
2. **Monthly minutes by app**:
   - Dograh: sum of call durations for non-realtime workflows. The `workflow_runs`
     durations in Dograh's Postgres give this.
   - Hoppwhistle: rows in the `transcriptions` table per month × `durationSec` × channels.
     The recent read-only `call volume report` script is a starting point.

CPU option for batch: Parakeet-TDT measured RTF ≈ 0.15 on 4 shared vCPU. A
dedicated CPU server in the €40–100/mo range (or spare capacity on an existing CPU
host such as the servers.com `netenroll` instance) moves the batch break-even down
to roughly **20k–45k stereo call-minutes/month**, at the cost of slower turnaround
for backlogs.

## 5. Recommendation
- **Batch moves first on cost grounds.** One GEX44 (≈ $211/mo) breaks even at about
  100k stereo call-minutes a month. The Recording Analyzer's all-history sweep alone
  can produce that volume. Batch also tolerates the European location and has the
  lowest risk.
- **Live moves when the volume justifies two US-East GPUs**, or when the benefits beyond
  cost matter: no per-minute exposure, data stays on our own infrastructure, and the
  model is under our control. Below about 200k live minutes a month, live HopSTT costs
  more than Cartesia ink-whisper. Make that call knowingly.
- Until volumes are known, rent hourly only for the benchmark (≈ $20–60). Do not commit
  to monthly hardware until §4 is filled in and the accuracy gate in `bench/` has passed.
