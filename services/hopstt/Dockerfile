# HopSTT GPU image (CUDA runtime, like services/hopvoice).
#
#   docker build -t hopstt:0.1.0 .                                  # NeMo + faster-whisper
#   docker build --build-arg HOPSTT_EXTRAS=qwen,fetch -t hopstt-qwen:0.1.0 .   # Qwen3-ASR (own image)
#
# Weights are NOT baked in by default: the compose `hopstt-fetch` service runs
# `python3 -m hopstt.tools.fetch_models` into the shared cache volume at the
# pinned revisions (or set HOPSTT_BAKE_MODELS to bake them into a layer).
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOPSTT_MODEL_CACHE=/models \
    HF_HOME=/models/.hf

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv python3-dev build-essential \
      libsndfile1 ffmpeg git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY hopstt ./hopstt

# CUDA 12.8 torch first so the extras do not pull a CPU or mismatched build.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
ARG HOPSTT_EXTRAS=nemo,whisper,vllm-client,gpu-metrics,fetch
RUN python3 -m pip install --break-system-packages --no-cache-dir Cython packaging \
    && python3 -m pip install --break-system-packages --no-cache-dir torch torchaudio --index-url ${TORCH_INDEX} \
    && python3 -m pip install --break-system-packages --no-cache-dir ".[${HOPSTT_EXTRAS}]"

RUN useradd --create-home --uid 10001 hopstt \
    && mkdir -p /models && chown -R hopstt:hopstt /models
USER hopstt

# Optional: bake pinned weights into the image (space separated model ids).
ARG HOPSTT_BAKE_MODELS=""
RUN if [ -n "${HOPSTT_BAKE_MODELS}" ]; then \
      python3 -m hopstt.tools.fetch_models --models ${HOPSTT_BAKE_MODELS}; \
    fi

EXPOSE 8790
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=5 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/readyz' % os.getenv('HOPSTT_PORT','8790'),timeout=4)"
# python -m hopstt (not bare uvicorn): the first SIGTERM drains (PROTOCOL §5).
STOPSIGNAL SIGTERM
CMD ["python3", "-m", "hopstt"]
