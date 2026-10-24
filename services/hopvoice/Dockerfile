FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv libsndfile1 sox ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY hopvoice ./hopvoice
# Benchmark/sweep/voice tools, runnable inside the GPU container:
#   docker compose -f docker-compose.gpu.yaml exec hopvoice python3 -m bench.sweep_stream ...
COPY tools ./tools
COPY bench ./bench

# qwen-streaming is the reproducible low-TTFA runtime. Set
# --build-arg HOPVOICE_QWEN_EXTRA=qwen to build against the official
# full-waveform wrapper instead.
ARG HOPVOICE_QWEN_EXTRA=qwen-streaming
RUN python3 -m pip install --break-system-packages --no-cache-dir ".[${HOPVOICE_QWEN_EXTRA}]"

RUN useradd --create-home --uid 10001 hopvoice
USER hopvoice

EXPOSE 8787
CMD ["python3", "-m", "uvicorn", "hopvoice.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
