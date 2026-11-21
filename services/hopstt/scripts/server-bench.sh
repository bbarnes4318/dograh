#!/usr/bin/env bash
# HopSTT benchmark on one of OUR servers (CPU; uses a GPU automatically only
# if you set BENCH_GPU=1 on a host with the NVIDIA container toolkit).
#
#   sudo bash server-bench.sh start [quick|full]   # runs in the background
#   sudo bash server-bench.sh status               # progress + last log lines
#   sudo bash server-bench.sh report               # print the latest summary
#   sudo bash server-bench.sh stop                 # stop and remove bench containers
#
# What it does (all inside Docker, nothing installed on the host):
#   1. records the host (CPU model, cores, RAM, GPU, load, running containers);
#   2. refuses to load the box while FreeSWITCH has live calls (FORCE=1 overrides);
#   3. builds HopSTT (CPU, NeMo + faster-whisper) and NVIDIA NeMo-Speech.cpp v0.3.0;
#   4. downloads the pinned model revisions (sha256-checked);
#   5. builds a public telephone-simulated corpus (FLEURS EN/ES, 8 kHz G.711);
#   6. measures batch accuracy (Parakeet-TDT v3, Whisper large-v3-turbo),
#      streaming accuracy + latency (Nemotron 3.5 on NeMo-Speech.cpp and on
#      PyTorch), and a concurrency ramp, sampling CPU/RAM of every container;
#   7. writes results under /opt/hopstt-bench/results/<timestamp>/.
#
# CPU use is capped with --cpus (BENCH_CPUS, default half the cores) and the
# containers run at low priority, so live services keep the rest of the box.
# No audio or transcript leaves the server; only model weights and the public
# FLEURS data are downloaded.
set -euo pipefail

ROOT=${BENCH_ROOT:-/opt/hopstt-bench}
BRANCH=${BENCH_BRANCH:-claude/quirky-volta-5njl4q}
NET=hopstt-bench
VOL=hopstt-bench-models
SRV=hopstt-bench-srv
CPP=hopstt-bench-cpp
CHUNK_CPP=${BENCH_CPP_CHUNK_MS:-160}
CHUNK_PT=${BENCH_PT_CHUNK_MS:-560}

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

cores=$(nproc)
CPUS=${BENCH_CPUS:-$(( cores / 2 > 2 ? cores / 2 : 2 ))}

stop_containers() {
  docker rm -f "$SRV" "$CPP" >/dev/null 2>&1 || true
  [ -f "$ROOT/sampler.pid" ] && kill "$(cat "$ROOT/sampler.pid")" 2>/dev/null || true
  rm -f "$ROOT/sampler.pid"
}

live_calls() {
  local fs
  fs=$(docker ps --format '{{.Names}}' | grep -m1 -i freeswitch || true)
  [ -z "$fs" ] && { echo 0; return; }
  docker exec "$fs" fs_cli -x "show calls count" 2>/dev/null | grep -oE '^[0-9]+' | head -1 || echo 0
}

record_host() {
  local f=$1
  {
    echo "== date"; date -u
    echo "== host"; hostname; uname -a
    echo "== cpu"; lscpu | grep -E 'Model name|^CPU\(s\)|Thread|Core|Socket|MHz|Flags' | sed 's/Flags:.*\(avx512[a-z_]*\).*/Flags: ... \1 .../'
    echo "== memory"; free -g
    echo "== disk"; df -h "$ROOT" /var/lib/docker 2>/dev/null
    echo "== gpu"; (command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv) || echo "no nvidia-smi: no NVIDIA GPU/driver"
    echo "== load"; uptime
    echo "== containers"; docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
    echo "== live calls (freeswitch)"; live_calls
  } > "$f" 2>&1
}

src_checkout() {
  local url=""
  for d in /opt/dograh-src /opt/dograh; do
    if [ -d "$d/.git" ]; then url=$(git -C "$d" remote get-url origin 2>/dev/null || true); [ -n "$url" ] && break; fi
  done
  url=${url:-https://github.com/bbarnes4318/dograh.git}
  if [ -d "$ROOT/src/.git" ]; then
    git -C "$ROOT/src" fetch -q --depth 1 origin "$BRANCH"
    git -C "$ROOT/src" checkout -q -f FETCH_HEAD
  else
    git clone -q --depth 1 --branch "$BRANCH" "$url" "$ROOT/src"
  fi
  git -C "$ROOT/src" rev-parse --short HEAD
}

bench() {  # run a bench tool in the HopSTT image on the bench network
  docker run --rm --network "$NET" --cpus 1 \
    -e HOPSTT_API_KEY="$KEY" -e HOPSTT_BENCH_OUT=/out \
    -v "$ROOT/src/services/hopstt:/w:ro" -v "$ROOT/data:/data" -v "$OUT:/out" \
    -w /w --user 0 hopstt-bench:cpu python -m "$@"
}

wait_ready() {  # url seconds
  local i
  for i in $(seq 1 "$2"); do
    if docker run --rm --network "$NET" hopstt-bench:cpu python -c \
       "import urllib.request,sys;urllib.request.urlopen('$1',timeout=3)" >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  return 1
}

run_all() {
  local profile=$1
  OUT=$ROOT/results/$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$OUT" "$ROOT/data"; chmod 700 "$ROOT" "$OUT"
  ln -sfn "$OUT" "$ROOT/results/latest"
  log "profile=$profile cpus=$CPUS/$cores out=$OUT"
  record_host "$OUT/host-before.txt"; cat "$OUT/host-before.txt"

  local calls; calls=$(live_calls)
  if [ "${calls:-0}" -gt 0 ] && [ "${FORCE:-0}" != 1 ]; then
    die "FreeSWITCH reports $calls live call(s). Run when calls are quiet, or FORCE=1."
  fi

  log "source"; echo "commit $(src_checkout)" | tee "$OUT/commit.txt"
  local H=$ROOT/src/services/hopstt

  log "build HopSTT CPU image (NeMo + faster-whisper)"
  nice -n 10 docker build -q -f "$H/Dockerfile.cpu" --build-arg HOPSTT_EXTRAS=nemo,whisper,fetch,nemo-cpp,bench \
    -t hopstt-bench:cpu "$H" >/dev/null
  log "build NeMo-Speech.cpp v0.3.0 (CPU)"
  nice -n 10 docker build -q -f "$H/Dockerfile.nemo-speech-cpp" -t hopstt-bench:nemo-cpp "$H" >/dev/null

  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create "$VOL" >/dev/null
  KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')

  # RAM guard: the PyTorch Nemotron + Whisper turbo engines need ~8 GB on top
  # of NeMo-Speech.cpp (~1.5 GB) and Parakeet (~3 GB). Skip them on small hosts.
  local avail_gb; avail_gb=$(awk '/MemAvailable/ {printf "%d", $2/1048576}' /proc/meminfo)
  ALLOWED="nemotron-3.5-streaming-cpp,parakeet-tdt-0.6b-v3"
  local models="parakeet-tdt-0.6b-v3"
  if [ "$avail_gb" -ge "${BENCH_MIN_GB_FULL:-14}" ]; then
    ALLOWED="$ALLOWED,nemotron-3.5-streaming,whisper-large-v3-turbo"
    models="$models nemotron-3.5-streaming whisper-large-v3-turbo"
  else
    log "only ${avail_gb} GB RAM available: skipping PyTorch Nemotron and Whisper turbo"
  fi
  log "fetch models: $models"
  docker run --rm --user 0 -v "$VOL:/models" -e HOPSTT_MODEL_CACHE=/models hopstt-bench:cpu \
    python -m hopstt.tools.fetch_models --models $models | tail -40 > "$OUT/models.json"

  local en=140 es=60
  [ "$profile" = quick ] && { en=42; es=18; }
  log "corpus: FLEURS telephone-simulated en=$en es=$es"
  docker run --rm --user 0 -v "$ROOT/src/services/hopstt:/w:ro" -v "$ROOT/data:/data" -w /w hopstt-bench:cpu \
    python -m bench.public_corpus --out "/data/fleurs-tel-$en-$es" --cache /data/fleurs-cache --en $en --es $es
  local CORPUS=/data/fleurs-tel-$en-$es

  log "start NeMo-Speech.cpp sidecar (Nemotron 3.5 q8_0, chunk ${CHUNK_CPP} ms)"
  stop_containers
  docker run -d --name "$CPP" --network "$NET" --cpus "$CPUS" --cpu-shares 256 \
    -v "$VOL:/models" -e NEMO_CPP_MODEL=nemotron-3.5 -e NEMO_CPP_CHUNK_MS="$CHUNK_CPP" \
    -e NEMO_CPP_API_KEY="$KEY" -e NEMO_CPP_SLOTS=256 hopstt-bench:nemo-cpp >/dev/null
  wait_ready "http://$CPP:8080/ready" 120 || { docker logs --tail 80 "$CPP"; die "NeMo-Speech.cpp not ready"; }

  log "start HopSTT (role=both, CPU)"
  docker run -d --name "$SRV" --network "$NET" --cpus "$CPUS" --cpu-shares 256 --user 0 \
    -v "$VOL:/models" \
    -e HOPSTT_ROLE=both -e HOPSTT_DEVICE=cpu -e HOPSTT_API_KEY="$KEY" -e HOPSTT_MODEL_CACHE=/models \
    -e HOPSTT_ALLOWED_MODELS="$ALLOWED" \
    -e HOPSTT_DEFAULT_STREAMING_MODEL=nemotron-3.5-streaming-cpp -e HOPSTT_DEFAULT_BATCH_MODEL=parakeet-tdt-0.6b-v3 \
    -e HOPSTT_NEMO_CPP_URL="ws://$CPP:8080/v1/audio/transcriptions/realtime" -e HOPSTT_NEMO_CPP_API_KEY="$KEY" \
    -e HOPSTT_NEMO_CPP_CHUNK_MS="$CHUNK_CPP" -e HOPSTT_MAX_STREAMS=256 -e HOPSTT_BATCH_MAX_CONCURRENCY=1 \
    -e HOPSTT_BATCH_PAUSE_AT_LIVE_UTIL=1.0 \
    hopstt-bench:cpu >/dev/null
  wait_ready "http://$SRV:8790/readyz" 180 || { docker logs --tail 80 "$SRV"; die "HopSTT not ready"; }

  log "resource sampler -> docker_stats.csv"
  ( while true; do
      docker stats --no-stream --format "$(date -u +%FT%TZ),{{.Name}},{{.CPUPerc}},{{.MemUsage}}" "$SRV" "$CPP" 2>/dev/null
      sleep 5
    done >> "$OUT/docker_stats.csv" ) &
  echo $! > "$ROOT/sampler.pid"

  local URL=http://$SRV:8790
  log "batch accuracy: parakeet-tdt-0.6b-v3, whisper-large-v3-turbo"
  local bmodels=parakeet-tdt-0.6b-v3; case "$ALLOWED" in *whisper*) bmodels="$bmodels,whisper-large-v3-turbo";; esac
  bench bench.run_batch --corpus "$CORPUS" --url "$URL" --models "$bmodels" \
    --out /out/batch > "$OUT/batch.log" 2>&1 || log "batch run failed (see batch.log)"

  log "stream accuracy + latency: Nemotron 3.5 on NeMo-Speech.cpp @${CHUNK_CPP} ms"
  bench bench.run_stream --corpus "$CORPUS" --url "$URL" --models nemotron-3.5-streaming-cpp \
    --chunk-ms "$CHUNK_CPP" --out /out/stream-cpp > "$OUT/stream-cpp.log" 2>&1 || log "stream-cpp failed (see log)"

  case "$ALLOWED" in *nemotron-3.5-streaming,*|*nemotron-3.5-streaming)
    log "stream accuracy + latency: Nemotron 3.5 on PyTorch @${CHUNK_PT} ms"
    local lim=""; [ "$profile" = quick ] && lim="--limit 30"
    bench bench.run_stream --corpus "$CORPUS" --url "$URL" --models nemotron-3.5-streaming \
      --chunk-ms "$CHUNK_PT" $lim --out /out/stream-pytorch > "$OUT/stream-pytorch.log" 2>&1 || log "stream-pytorch failed (see log)" ;;
  esac

  local steps="1,5,10,25,50,100,200"; [ "$profile" = quick ] && steps="1,5,10,25,50"
  log "capacity ramp (NeMo-Speech.cpp, steps $steps, stops at first breach)"
  bench bench.capacity --mode live --corpus "$CORPUS" --url "$URL" --model nemotron-3.5-streaming-cpp \
    --chunk-ms "$CHUNK_CPP" --steps "$steps" --step-duration 45 --pool-size 20 \
    --max-p95-final-ms 300 --out /out/capacity-cpp > "$OUT/capacity-cpp.log" 2>&1 || log "capacity failed (see log)"

  log "report"
  bench bench.report --corpus "$CORPUS" --runs /out --baseline none --min-items 0 --min-es-items 0 \
    --min-silence-items 0 --out /out/report > "$OUT/report.log" 2>&1 || log "report failed (see report.log)"

  record_host "$OUT/host-after.txt"
  stop_containers
  log "done: $OUT"
  print_report
}

print_report() {
  local d=$ROOT/results/latest
  [ -d "$d" ] || die "no results yet"
  echo "===== $(readlink -f "$d")"
  sed -n '1,/== containers/p' "$d/host-before.txt" 2>/dev/null | head -40
  [ -f "$d/report/summary.md" ] && cat "$d/report/summary.md"
  for f in "$d"/capacity-cpp/summary.md "$d"/capacity-cpp/*.md; do [ -f "$f" ] && { echo "----- $f"; cat "$f"; break; }; done
  if [ -f "$d/docker_stats.csv" ]; then
    echo "----- peak CPU/RAM per container (docker stats, 5 s samples)"
    awk -F, '{gsub("%","",$3); split($4,m," "); if ($3+0>cpu[$2]) cpu[$2]=$3+0; if (!(($2) in mem) || m[1]>mem[$2]) mem[$2]=m[1]} END {for (c in cpu) printf "%s peak_cpu=%.0f%% (100%% = 1 core) peak_mem=%s\n", c, cpu[c], mem[c]}' "$d/docker_stats.csv"
  fi
  echo "----- logs"; tail -n 3 "$d"/*.log 2>/dev/null
}

cmd=${1:-start}
case "$cmd" in
  start)
    mkdir -p "$ROOT/results"
    if [ "${BENCH_FOREGROUND:-0}" = 1 ]; then run_all "${2:-quick}"; exit; fi
    nohup env BENCH_FOREGROUND=1 bash "$0" start "${2:-quick}" > "$ROOT/run.log" 2>&1 &
    echo "started (pid $!). Progress: bash $0 status    Result: bash $0 report"
    ;;
  status) tail -n 25 "$ROOT/run.log" 2>/dev/null || echo "not started"; docker ps --filter name=hopstt-bench --format '{{.Names}} {{.Status}}' ;;
  report) print_report ;;
  stop) stop_containers; pkill -f "server-bench.sh start" || true; echo stopped ;;
  *) die "usage: $0 start [quick|full] | status | report | stop" ;;
esac
