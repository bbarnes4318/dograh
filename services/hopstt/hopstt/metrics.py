"""Prometheus metrics. Every label is from a small fixed set: model id (from
the allow-list), role, kind, reason / error code, status, language
(en|es|auto|other). Never tenant, call, session or correlation ids."""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger("hopstt.metrics")

LATENCY_BUCKETS = (0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)
STEP_BUCKETS = (0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56)
BATCH_SIZE_BUCKETS = (1, 2, 4, 8, 16, 32, 64, 128, 256)
RTF_BUCKETS = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0)
JOB_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900)
LAG_BUCKETS = (50, 100, 250, 500, 1000, 2000, 3000, 5000)


class Metrics:
    def __init__(self, role: str):
        self.role = role
        r = self.registry = CollectorRegistry(auto_describe=True)
        self.active_streams = Gauge("hopstt_active_streams", "Open live streams", ["model"], registry=r)
        self.capacity_available = Gauge(
            "hopstt_live_capacity_available", "Live streams that can still be admitted", registry=r
        )
        self.draining = Gauge("hopstt_draining", "1 while the worker drains", registry=r)
        self.ready = Gauge("hopstt_ready", "1 when /readyz would return 200", registry=r)
        self.stream_opens = Counter(
            "hopstt_stream_opens_total", "Accepted live streams", ["model", "language"], registry=r
        )
        self.stream_reconnects = Counter(
            "hopstt_stream_reconnect_opens_total",
            "Streams opened with generation > 0 (client reconnects)",
            ["model"],
            registry=r,
        )
        self.stream_closes = Counter(
            "hopstt_stream_closes_total", "Closed live streams by reason", ["reason"], registry=r
        )
        self.stream_rejects = Counter(
            "hopstt_stream_rejects_total", "Streams refused before start", ["reason"], registry=r
        )
        self.partial_latency = Histogram(
            "hopstt_partial_latency_seconds",
            "Chunk complete (enqueued) -> interim sent",
            ["model"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.final_latency = Histogram(
            "hopstt_final_latency_seconds",
            "finalize received -> final sent",
            ["model"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.step_seconds = Histogram(
            "hopstt_inference_step_seconds",
            "Wall time of one batched streaming step",
            ["model"],
            buckets=STEP_BUCKETS,
            registry=r,
        )
        self.step_batch_size = Histogram(
            "hopstt_inference_batch_size",
            "Sessions per streaming step",
            ["model"],
            buckets=BATCH_SIZE_BUCKETS,
            registry=r,
        )
        self.step_rtf = Histogram(
            "hopstt_inference_step_rtf",
            "Step wall time / audio seconds advanced (per session)",
            ["model"],
            buckets=RTF_BUCKETS,
            registry=r,
        )
        self.queue_audio_ms = Gauge(
            "hopstt_live_queue_audio_ms", "Queued live audio across sessions (ms)", registry=r
        )
        self.lag_ms = Histogram(
            "hopstt_live_lag_ms", "Per-session lag samples (ms)", buckets=LAG_BUCKETS, registry=r
        )
        self.stream_errors = Counter(
            "hopstt_stream_errors_total", "Errors sent to stream clients", ["code"], registry=r
        )
        self.audio_seconds = Counter(
            "hopstt_audio_seconds_total",
            "Audio seconds processed (for cost per minute)",
            ["kind", "model"],
            registry=r,
        )
        self.batch_jobs = Counter(
            "hopstt_batch_jobs_total", "Batch requests by final status", ["kind", "status"], registry=r
        )
        self.batch_queue_depth = Gauge(
            "hopstt_batch_queue_depth", "Batch jobs waiting", ["priority"], registry=r
        )
        self.batch_running = Gauge("hopstt_batch_running", "Batch jobs running", registry=r)
        self.batch_inference_seconds = Histogram(
            "hopstt_batch_inference_seconds",
            "Batch inference wall time",
            ["model"],
            buckets=JOB_BUCKETS,
            registry=r,
        )
        self.batch_queue_seconds = Histogram(
            "hopstt_batch_queue_seconds", "Batch time spent queued", buckets=JOB_BUCKETS, registry=r
        )
        self.batch_retries = Counter("hopstt_batch_retries_total", "Batch retry attempts", registry=r)
        self.cuda_memory_allocated = Gauge(
            "hopstt_cuda_memory_allocated_bytes", "torch.cuda.memory_allocated", registry=r
        )
        self.cuda_memory_reserved = Gauge(
            "hopstt_cuda_memory_reserved_bytes", "torch.cuda.memory_reserved", registry=r
        )
        self.gpu_utilization = Gauge("hopstt_gpu_utilization_percent", "NVML GPU utilisation", registry=r)
        self.gpu_memory_used = Gauge("hopstt_gpu_memory_used_bytes", "NVML used memory", registry=r)
        self._nvml_handle = None
        self._nvml_tried = False

    def refresh_device(self, device: str) -> None:
        """Best effort; torch/pynvml are optional."""
        if not device.startswith("cuda"):
            return
        try:
            import sys

            torch = sys.modules.get("torch")
            if torch is not None and torch.cuda.is_available():
                self.cuda_memory_allocated.set(torch.cuda.memory_allocated())
                self.cuda_memory_reserved.set(torch.cuda.memory_reserved())
        except Exception:  # pragma: no cover - GPU only
            pass
        try:
            if not self._nvml_tried:
                self._nvml_tried = True
                import pynvml

                pynvml.nvmlInit()
                index = int(device.split(":", 1)[1]) if ":" in device else 0
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            if self._nvml_handle is not None:
                import pynvml

                util = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                self.gpu_utilization.set(util.gpu)
                self.gpu_memory_used.set(mem.used)
        except Exception:  # pragma: no cover - GPU only
            self._nvml_handle = None

    def render(self) -> bytes:
        return generate_latest(self.registry)
