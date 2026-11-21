#!/usr/bin/env bash
# Fetch the pinned GGUF (sha256-verified) and start `nemo-speech serve`.
#
# Env:
#   NEMO_CPP_MODEL        nemotron-3.5 (default) | nemotron-en
#   NEMO_CPP_CHUNK_MS     80|160|320|560|1120 (default 160); must equal
#                         HOPSTT_NEMO_CPP_CHUNK_MS on the HopSTT side
#   NEMO_CPP_API_KEY      bearer key HopSTT sends (required unless
#                         NEMO_CPP_INSECURE=true; keep the port private anyway)
#   NEMO_CPP_GPU          -1 = CPU (default), else GPU index (CUDA build only)
#   NEMO_CPP_SLOTS        concurrent stream state slots (default 64)
#   NEMO_CPP_PORT         default 8080; NEMO_CPP_HOST default 0.0.0.0
set -euo pipefail

model="${NEMO_CPP_MODEL:-nemotron-3.5}"
case "$model" in
  nemotron-3.5)
    repo=nvidia/nemotron-3.5-asr-streaming-0.6b
    rev=ea30d66debe3740a08b573244286791d423d6b3e
    file=nemotron-3.5-asr-streaming-0.6b.q8_0.gguf
    sha=3fc991d3badad7277c11030a7519832cddaf2057aafed6d4b25147e953a070b1 ;;
  nemotron-en)
    repo=nvidia/nemotron-speech-streaming-en-0.6b
    rev=ebe59e5a817142986528bbbee5dba8db7b38ed50
    file=nemotron-speech-streaming-en-0.6b.q8_0.gguf
    sha=d9a01898d2a611c8764e23a1c2f45e70bbd5a425dc4de93692ac951dd603812d ;;
  *) echo "unknown NEMO_CPP_MODEL=$model" >&2; exit 2 ;;
esac

case "${NEMO_CPP_CHUNK_MS:-160}" in
  80) rc=0 ;; 160) rc=1 ;; 320) rc=3 ;; 560) rc=6 ;; 1120) rc=13 ;;
  *) echo "NEMO_CPP_CHUNK_MS must be 80|160|320|560|1120" >&2; exit 2 ;;
esac

dir=/models/gguf/$rev
path=$dir/$file
mkdir -p "$dir"
if [ ! -f "$path" ] || ! echo "$sha  $path" | sha256sum -c --quiet - 2>/dev/null; then
  echo "downloading $repo@$rev/$file" >&2
  curl -fsSL --retry 4 -o "$path.part" "https://huggingface.co/$repo/resolve/$rev/$file"
  echo "$sha  $path.part" | sha256sum -c --quiet -
  mv "$path.part" "$path"
fi

args=(serve --host "${NEMO_CPP_HOST:-0.0.0.0}" --port "${NEMO_CPP_PORT:-8080}"
      --asr.model.path "$path" --gpu "${NEMO_CPP_GPU:--1}" --no-ui
      --asr.streaming.rnnt_right_context "$rc"
      --asr.batching.state_arena_slots "${NEMO_CPP_SLOTS:-64}")
if [ -n "${NEMO_CPP_API_KEY:-}" ]; then
  args+=(--api-key "$NEMO_CPP_API_KEY")
elif [ "${NEMO_CPP_INSECURE:-false}" != "true" ]; then
  echo "set NEMO_CPP_API_KEY (or NEMO_CPP_INSECURE=true for local tests)" >&2; exit 2
fi
exec nemo-speech "${args[@]}" "$@"
