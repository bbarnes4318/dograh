# HopSTT monitoring and operations

## Health endpoints
| Endpoint | Use it for | Notes |
|---|---|---|
| `/healthz` | Container liveness (restart if it fails) | Never touches the GPU |
| `/readyz` | Load balancer / compose healthcheck / client failover | 503 while loading, warming, draining, or at capacity |
| `/v1/capacity` | Load-aware routing (the Dograh client ranks endpoints by it), dashboards | Needs the bearer key |
| `/metrics` | Prometheus | No auth: bind it to a private interface only |

`/readyz` returning 200 is **not** a deployment check by itself. After a deploy, run
`python -m hopstt.tools.smoke` (streams a WAV like a live call and reports
finalize→final latency).

## Prometheus
Add to the existing `prometheus.yml` (hoppwhistle `infra/`, which already scrapes
the host):

```yaml
- job_name: hopstt
  static_configs:
    - targets: ["<live-host-private-ip>:8790", "<batch-host-private-ip>:8791"]
```

Labels are bounded (`model`, `role`, `kind`, `reason`, `language` ∈ en/es/auto/other).
Metrics carry no call, tenant or phone identifiers.

## Key metrics
| Metric | Meaning |
|---|---|
| `hopstt_active_streams{model}` / `hopstt_live_capacity_available` | Live load and remaining headroom |
| `hopstt_stream_rejects_total{reason}` | `capacity` / `draining` rejects. Any non-zero rate means calls are failing over |
| `hopstt_stream_reconnect_opens_total` | Opens with generation > 0, i.e. client recoveries |
| `hopstt_stream_errors_total{reason}` / `hopstt_stream_closes_total{reason}` | `overloaded`, `model_error`, `idle_timeout`, … |
| `hopstt_partial_latency_seconds`, `hopstt_final_latency_seconds` | Server-side partial latency and finalize→final latency |
| `hopstt_inference_step_seconds`, `hopstt_inference_step_rtf`, `hopstt_inference_batch_size` | GPU step cost; RTF ≥ 1 means falling behind |
| `hopstt_live_queue_audio_ms`, `hopstt_live_lag_ms` | Audio waiting / how far sessions trail real time |
| `hopstt_batch_queue_depth`, `hopstt_batch_running`, `hopstt_batch_jobs_total{status}`, `hopstt_batch_queue_seconds`, `hopstt_batch_inference_seconds`, `hopstt_batch_retries_total` | Batch throughput and backlog |
| `hopstt_audio_seconds_total{kind,model}` | Audio processed. Divide monthly infrastructure cost by this for $/minute |
| `hopstt_gpu_utilization_percent`, `hopstt_gpu_memory_used_bytes`, `hopstt_cuda_memory_*` | GPU (NVML / torch) |
| `hopstt_ready`, `hopstt_draining` | State |

Client side:
- **Dograh** logs every reconnect (generation, endpoint host, error code, recovery ms)
  and every paid fallback (WARNING plus daily counter).
- **Hoppwhistle** logs paid-fallback use as WARN with engine and reason, plus a
  daily-minutes counter.

## Alerts (starting thresholds; retune after the capacity test)
```yaml
- alert: HopSTTLiveNearCapacity        # act before calls are affected
  expr: min(hopstt_live_capacity_available) / max(hopstt_active_streams + hopstt_live_capacity_available) < 0.3
  for: 5m
- alert: HopSTTLiveRejecting
  expr: sum(rate(hopstt_stream_rejects_total[5m])) > 0
  for: 2m
- alert: HopSTTFinalLatencyHigh
  expr: histogram_quantile(0.95, sum by (le) (rate(hopstt_final_latency_seconds_bucket[5m]))) > 0.3
  for: 10m
- alert: HopSTTFallingBehind
  expr: histogram_quantile(0.95, sum by (le) (rate(hopstt_inference_step_rtf_bucket[5m]))) > 0.8
  for: 5m
- alert: HopSTTNotReady
  expr: hopstt_ready == 0
  for: 2m
- alert: HopSTTBatchBacklog
  expr: hopstt_batch_queue_depth > 50
  for: 30m
```
For paid-fallback use, alert on any occurrence (Dograh and Hoppwhistle log warnings).

## Runbook
- **Capacity alert:** add a live instance (another GPU) and append it to the Dograh
  endpoint list (`HOPSTT_DEFAULT_ENDPOINT=http://a:8790,http://b:8790`). Clients route
  by `/v1/capacity`, so nothing else changes.
- **Latency alert with low load:** check `inference_step_seconds` and GPU memory, and
  look for a noisy neighbour if the GPU is shared with Hopvoice. Consider a larger
  `chunk_ms` only as a measured tradeoff.
- **Deploy or upgrade:** build the new image tag, keep `HOPSTT_MODEL_REVISIONS`
  pinned, and restart one live instance at a time. SIGTERM drains for up to
  `HOPSTT_DRAIN_TIMEOUT_S` (600 s). Run the smoke test, then move on to the next one.
- **Rollback:** redeploy the previous image tag. The model cache is keyed by revision,
  so old weights are still on disk.
- **Backups:** none required. The service is stateless: jobs and results live in
  memory with a TTL. The model cache can be re-downloaded with
  `hopstt.tools.fetch_models` (verifies sha256).
