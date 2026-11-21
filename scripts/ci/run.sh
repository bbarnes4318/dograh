#!/usr/bin/env bash
# dograh local CI -- the replacement for the GitHub Actions checks
# (api-tests.yml, pre-pr-drift-check.yml, hopvoice-tests.yml) plus the
# HopSTT service tests.
#
#   ./scripts/ci/run.sh fast        # before every PR: all checks (~5 min)
#   ./scripts/ci/run.sh <check>     # one of: drift api hopvoice hopstt
#   ./scripts/ci/run.sh clean       # remove containers a killed run left behind
#
# Exit codes: 0 PASS, 1 FAIL, 2 usage, 3 INCOMPLETE (a required check could
# not run here -- never a pass), 130 interrupted.
#
# Needs Python 3.13 (and 3.12 for Hopvoice/HopSTT), Node 22, Docker, and the
# pipecat submodule (fetched automatically if missing).
# Reports: .ci-reports/<time>-<profile>-<sha>/summary.md (newest 20 kept).
# Documentation: scripts/ci/README.md
set -euo pipefail
shopt -s inherit_errexit

# ── Scrub the environment ───────────────────────────────────────────────
# Re-exec under `env -i` with an allowlist: provider API keys, DATABASE_URL,
# SSH agent sockets and everything else in the calling shell are gone before
# any check runs. The names (never values) of credential-like variables are
# recorded; with CI_FORBID_SECRETS=1 their presence fails the run.
if [[ "${DGCI_SCRUBBED:-}" != 1 ]]; then
  sensitive=$(env | cut -d= -f1 | grep -E \
    '(^|_)(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|API_KEY|APIKEY|ACCESS_KEY|CREDENTIALS?)($|_)|^(OPENAI|ANTHROPIC|DEEPGRAM|ELEVENLABS|CARTESIA|AZURE|AWS|TWILIO|VONAGE|TELNYX|SLACK|STRIPE|DEPLOY)_|^(SSH_AUTH_SOCK|DATABASE_URL|REDIS_URL|PGPASSWORD|GITHUB_TOKEN|GH_TOKEN)$' |
    sort | tr '\n' ' ' || true)
  keep=(DGCI_SCRUBBED=1 "DGCI_SENSITIVE=${sensitive% }")
  for v in PATH HOME USER LANG LC_ALL TERM TZ TMPDIR NO_COLOR \
    HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy \
    NODE_EXTRA_CA_CERTS SSL_CERT_FILE REQUESTS_CA_BUNDLE DOCKER_HOST XDG_CACHE_HOME UV_CACHE_DIR; do
    [[ -n "${!v:-}" ]] && keep+=("$v=${!v}")
  done
  while IFS='=' read -r name _; do
    case "$name" in CI_NETRC_*) ;; CI_*) keep+=("$name=${!name}") ;; esac
  done < <(env)
  exec env -i "${keep[@]}" bash "$0" "$@"
fi

[[ -n "${CI_NODE_DIR:-}" ]] && export PATH="$CI_NODE_DIR:$PATH"
REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$REPO"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' B=$'\e[34m' D=$'\e[2m' O=$'\e[0m'; else R='' G='' Y='' B='' D='' O=''; fi
log() { printf '%s[ci %s]%s %s\n' "$D" "$(date +%H:%M:%S)" "$O" "$*" >&2; }

PROFILE="${1:-}"
case "$PROFILE" in
  fast | drift | api | hopvoice | hopstt) ;;
  clean)
    docker ps -aq --filter label=dograh-ci=1 | xargs -r docker rm -f
    exit 0
    ;;
  -h | --help | help | '')
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "unknown profile '$PROFILE' (fast|drift|api|hopvoice|hopstt|clean)" >&2
    exit 2
    ;;
esac

SHA="$(git rev-parse HEAD)"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$PROFILE-${SHA:0:8}-$$"
REPORTS="${CI_REPORTS_DIR:-$REPO/.ci-reports}"
RD="$REPORTS/$RUN_ID"
mkdir -p "$RD/steps"
: >"$RD/steps.tsv"
: >"$RD/notes.txt"
START=$(date +%s)
SEQ=0
CRIT=''
[[ -n "$(git status --porcelain --untracked-files=no -- . ':!pipecat')" ]] && DIRTY=yes || DIRTY=no
PY="${CI_PYTHON:-python3.13}"
PY_HOPVOICE="${CI_PYTHON_HOPVOICE:-python3.12}"
PY_HOPSTT="${CI_PYTHON_HOPSTT:-python3.12}"
VENV="$REPO/.venv"
HV_VENV="$REPO/.ci-reports/.hopvoice-venv"
HS_VENV="$REPO/.ci-reports/.hopstt-venv"

# ── Cleanup and report, whatever happens ────────────────────────────────
INTERRUPTED=''
finish() {
  local rc=$? status code
  trap - EXIT INT TERM
  [[ -f "$RD/.sid" ]] && kill -TERM -- "-$(cat "$RD/.sid")" 2>/dev/null || true
  # The OpenAPI check regenerates the spec in place; put the original back.
  [[ -f "$RD/.openapi.orig" ]] && cp "$RD/.openapi.orig" docs/api-reference/openapi.json && rm -f "$RD/.openapi.orig"
  local ids
  ids=$(docker ps -aq --filter "label=dograh-ci.run=$RUN_ID" 2>/dev/null || true)
  if [[ -n "$ids" ]]; then
    for id in $ids; do docker logs --tail 200 "$id" >"$RD/container-$id.log" 2>&1 || true; done
    echo "$ids" | xargs -r docker rm -f >/dev/null 2>&1 || true
  fi

  local nfail nblock npass
  nfail=$(awk -F'\t' '$2=="FAIL"||$2=="TIMEOUT"' "$RD/steps.tsv" | wc -l)
  nblock=$(awk -F'\t' '$2=="BLOCKED"' "$RD/steps.tsv" | wc -l)
  npass=$(awk -F'\t' '$2=="PASS"' "$RD/steps.tsv" | wc -l)
  if [[ -n "$INTERRUPTED" ]]; then status=INTERRUPTED code=130
  elif ((nfail > 0)); then status=FAIL code=1
  elif ((nblock > 0)); then status=INCOMPLETE code=3
  elif ((rc != 0)); then status=FAIL code=1; echo "runner exited $rc outside a step" >>"$RD/notes.txt"
  elif ((npass == 0)); then status=INCOMPLETE code=3; echo "no step ran" >>"$RD/notes.txt"
  else status=PASS code=0; fi
  local dur=$(($(date +%s) - START))
  {
    echo "# dograh CI -- $PROFILE -- $status"
    echo
    echo "commit \`$SHA\` · uncommitted changes: $DIRTY · $((dur / 60))m$((dur % 60))s"
    echo
    echo "| step | status | time | note |"
    echo "|---|---|---|---|"
    awk -F'\t' '{printf "| %s | **%s** | %ss | %s |\n", $1, $2, $3, $5}' "$RD/steps.tsv"
    if [[ -s "$RD/notes.txt" ]]; then echo; echo "Notes:"; sed 's/^/- /' "$RD/notes.txt"; fi
    for f in $(awk -F'\t' '$2=="FAIL"||$2=="TIMEOUT" {print $4}' "$RD/steps.tsv"); do
      echo; echo "### $f"; echo '```'; tail -n 40 "$RD/$f" || true; echo '```'
    done
  } >"$RD/summary.md"
  printf '{"status":"%s","profile":"%s","sha":"%s","durationSeconds":%d}\n' "$status" "$PROFILE" "$SHA" "$dur" >"$RD/summary.json"
  ls -1dt "$REPORTS"/*/ 2>/dev/null | tail -n +21 | xargs -r rm -rf || true
  local c=$G
  [[ $status == FAIL || $status == INTERRUPTED ]] && c=$R
  [[ $status == INCOMPLETE ]] && c=$Y
  printf '\n%s══ %s: %s ══%s  %d passed, %d failed, %d blocked in %dm%02ds\n   report: %s\n' \
    "$c" "$PROFILE" "$status" "$O" "$npass" "$nfail" "$nblock" $((dur / 60)) $((dur % 60)) "$RD/summary.md" >&2
  [[ $status == INCOMPLETE ]] && echo "   INCOMPLETE is not a pass: some required checks could not run here." >&2
  exit "$code"
}
trap finish EXIT
trap 'INTERRUPTED=1; exit 130' INT TERM

record() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "${5:-}" >>"$RD/steps.tsv"; }
blocked() {
  printf '  %s%-8s%s %s (%s)\n' "$Y" BLOCKED "$O" "$1" "$2" >&2
  record "$1" BLOCKED 0 - "$2"
}

# step [--critical] <name> <timeout-seconds> <function>
step() {
  local crit=''
  [[ "$1" == --critical ]] && crit=1 && shift
  local name="$1" limit="$2"
  shift 2
  if [[ -n "$CRIT" ]]; then blocked "$name" "not run: critical step '$CRIT' failed"; return 0; fi
  SEQ=$((SEQ + 1))
  local logf
  logf=$(printf 'steps/%02d-%s.log' "$SEQ" "$name")
  printf '  %s%-8s%s %s\n' "$B" RUN "$O" "$name" >&2
  local t0=$SECONDS rc=0
  setsid timeout --kill-after=20 "$limit" bash -c "set -euo pipefail; shopt -s inherit_errexit; set -x; $*" >"$RD/$logf" 2>&1 &
  local pid=$!
  echo "$pid" >"$RD/.sid"
  wait "$pid" || rc=$?
  rm -f "$RD/.sid"
  local secs=$((SECONDS - t0)) st=PASS note=''
  ((rc != 0)) && st=FAIL note="exit $rc"
  ((rc == 124)) && st=TIMEOUT note="exceeded ${limit}s"
  local c=$G
  [[ $st != PASS ]] && c=$R
  printf '  %s%-8s%s %s %s(%ss)%s\n' "$c" "$st" "$O" "$name" "$D" "$secs" "$O" >&2
  if [[ $st != PASS ]]; then
    tail -n 20 "$RD/$logf" | sed 's/^/      | /' >&2
    [[ -n "$crit" ]] && CRIT="$name"
  fi
  record "$name" "$st" "$secs" "$logf" "$note"
}

# ── Preflight ───────────────────────────────────────────────────────────
log "profile=$PROFILE commit=${SHA:0:12} report=$RD"
if [[ -n "${DGCI_SENSITIVE:-}" ]]; then
  echo "credential-like variables received and dropped (names only): $DGCI_SENSITIVE" >>"$RD/notes.txt"
  if [[ "${CI_FORBID_SECRETS:-}" == 1 ]]; then
    record secret-exposure FAIL 0 - "CI_FORBID_SECRETS=1 and this job was handed: $DGCI_SENSITIVE"
    exit 0
  fi
fi
if [[ $PROFILE != hopvoice && $PROFILE != hopstt ]]; then
  command -v "$PY" >/dev/null || { blocked python "$PY not found (the API tests and drift check use Python 3.13; set CI_PYTHON)"; exit 0; }
  nm=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo none)
  [[ "$nm" == "${CI_NODE_MAJOR:-22}" ]] || { blocked node-version "node $nm on PATH; the workflows used Node 22 (set CI_NODE_DIR)"; exit 0; }
fi
if [[ $PROFILE == hopvoice || $PROFILE == fast ]]; then
  command -v "$PY_HOPVOICE" >/dev/null || { blocked python3.12 "$PY_HOPVOICE not found (hopvoice-tests.yml used Python 3.12; set CI_PYTHON_HOPVOICE)"; exit 0; }
fi
if [[ $PROFILE == hopstt || $PROFILE == fast ]]; then
  command -v "$PY_HOPSTT" >/dev/null || { blocked python3.12 "$PY_HOPSTT not found (the HopSTT service tests use Python 3.12; set CI_PYTHON_HOPSTT)"; exit 0; }
fi
if [[ $PROFILE == api || $PROFILE == fast ]] && ! docker info >/dev/null 2>&1; then
  blocked docker "Docker is required (Postgres+pgvector and Redis run as throwaway containers) and the daemon is not reachable"
  exit 0
fi

mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/dograh-ci"
exec {LOCK}>"${XDG_CACHE_HOME:-$HOME/.cache}/dograh-ci/run.lock"
if ! flock -n "$LOCK"; then
  log "another dograh CI run holds the lock; waiting (up to ${CI_QUEUE_TIMEOUT:-3600}s)"
  flock -w "${CI_QUEUE_TIMEOUT:-3600}" "$LOCK" || { blocked queue "lock not free in time"; exit 0; }
fi

# ── Checks ──────────────────────────────────────────────────────────────

do_setup() {
  # The pipecat submodule, at the commit this checkout pins.
  if git submodule status pipecat | grep -q '^-'; then
    git submodule update --init --depth 1 pipecat
  fi
  git submodule status pipecat
  # .venv (gitignored), built with Python 3.13 as the workflows did, then the
  # repo's own dependency script. Re-running it is cheap when nothing changed.
  if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
    rm -rf "$VENV"
    "$PY" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  ./scripts/setup_requirements.sh --dev
}

do_drift() {
  # pre-pr-drift-check.yml, read-only. The workflow ran scripts/format.sh,
  # which REWRITES files, and failed if that changed api/ or ui/. That is fine
  # on a throwaway CI checkout and wrong in someone's working tree, so the
  # same rules run here in check mode:
  #   ruff check --select I,F401 (no --fix) + ruff format --check   == format.sh on api/
  #   eslint --fix-dry-run, failing if any file would change          == npm run fix-lint
  # (format.sh also reformats pipecat/; the workflow never checked that.)
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  local bad=0
  ruff check api --select I --select F401 || { echo "ruff check: run ./scripts/format.sh and commit"; bad=1; }
  ruff format --check api || { echo "ruff format: run ./scripts/format.sh and commit"; bad=1; }

  (cd ui && npm ci --no-audit --no-fund)
  local out="$RD/eslint.json" erc=0
  (cd ui && npx eslint --fix-dry-run --format json . --ignore-pattern '.next/*' \
    --ignore-pattern 'node_modules/*' --ignore-pattern 'next-env.d.ts' >"$out") || erc=$?
  node -e '
    const r = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
    const fixable = r.filter(f => f.output !== undefined).map(f => f.filePath);
    const errors = r.reduce((n, f) => n + f.errorCount, 0);
    if (fixable.length) console.log("UI files fix-lint would change:\n  " + fixable.join("\n  "));
    if (errors) console.log(errors + " ESLint error(s) fix-lint cannot fix");
    process.exit(fixable.length || errors ? 1 : 0);
  ' "$out" || { echo "UI lint: run npm run fix-lint inside ui/ and commit"; bad=1; }
  ((erc == 0 || erc == 1)) || { echo "eslint itself failed (exit $erc)"; bad=1; }

  # OpenAPI: regenerate exactly as the workflow did, compare, restore.
  cp docs/api-reference/openapi.json "$RD/.openapi.orig"
  DATABASE_URL=postgresql+asyncpg://dummy:dummy@localhost/dummy REDIS_URL=redis://localhost:6379/0 \
    ENABLE_AWS_S3=false MINIO_PUBLIC_ENDPOINT=http://localhost:9000 DEPLOYMENT_MODE=oss \
    python -u -m scripts.dump_docs_openapi
  if ! cmp -s docs/api-reference/openapi.json "$RD/.openapi.orig"; then
    echo "docs/api-reference/openapi.json is out of sync with the FastAPI app."
    echo "Run 'python -m scripts.dump_docs_openapi' and commit the result."
    bad=1
  fi
  cp "$RD/.openapi.orig" docs/api-reference/openapi.json
  rm -f "$RD/.openapi.orig"
  return "$bad"
}

do_api() {
  # api-tests.yml: the whole pytest suite against Postgres 17 + pgvector and
  # Redis 7, both throwaway containers on random loopback ports. The test
  # setup (api/conftest.py) creates test_db and runs the alembic migrations.
  local p="dgci-$RUN_ID"
  docker run -d --name "$p-pg" --label dograh-ci=1 --label "dograh-ci.run=$RUN_ID" --memory 2g \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=postgres \
    -p 127.0.0.1::5432 "${CI_PG_IMAGE:-pgvector/pgvector:pg17}" -c fsync=off -c synchronous_commit=off >/dev/null
  docker run -d --name "$p-redis" --label dograh-ci=1 --label "dograh-ci.run=$RUN_ID" --memory 512m \
    -p 127.0.0.1::6379 redis:7 redis-server --save '' --appendonly no >/dev/null
  local pg rd
  pg=$(docker port "$p-pg" 5432/tcp | head -1 | sed 's/.*://')
  rd=$(docker port "$p-redis" 6379/tcp | head -1 | sed 's/.*://')
  for _ in $(seq 1 60); do
    docker exec "$p-pg" psql -U postgres -tAc 'select 1' >/dev/null 2>&1 && break
    sleep 1
  done
  sleep 1
  echo "postgres (pgvector) 127.0.0.1:$pg, redis 127.0.0.1:$rd"

  (cd api/mcp_server/ts_validator && npm install --no-audit --no-fund)
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  local junit="$RD/pytest-api.xml" rc=0
  # No -x: report every failure, not just the first.
  (cd api && DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:$pg/test_db" \
    REDIS_URL="redis://127.0.0.1:$rd/0" ENABLE_AWS_S3=false MINIO_PUBLIC_ENDPOINT=http://localhost:9000 \
    DEPLOYMENT_MODE=oss ENVIRONMENT=test LOG_LEVEL=INFO PYTHONPATH="$REPO" \
    python -m pytest tests/ -q -rs -p no:cacheprovider --junitxml="$junit") || rc=$?
  # A run that collected nothing, or skipped what it was given services for,
  # must not read as a pass.
  python - "$junit" "$RD/notes.txt" <<'PY' || rc=1
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot()
s = r if r.tag == "testsuite" else r.find("testsuite")
tests, fails, errs, skips = (int(s.get(k, 0)) for k in ("tests", "failures", "errors", "skipped"))
line = f"api suite: {tests} tests, {tests - fails - errs - skips} passed, {fails + errs} failed, {skips} skipped"
print(line); open(sys.argv[2], "a").write(line + "\n")
if tests == 0: print("no tests ran"); sys.exit(1)
if skips: print("tests were skipped although Postgres and Redis were supplied"); sys.exit(1)
PY
  return "$rc"
}

do_hopvoice() {
  # hopvoice-tests.yml: Python 3.12, the no-GPU contract tests.
  if [[ ! -x "$HV_VENV/bin/python" ]]; then "$PY_HOPVOICE" -m venv "$HV_VENV"; fi
  # shellcheck disable=SC1091
  source "$HV_VENV/bin/activate"
  python -m pip install -q -e 'services/hopvoice[test]'
  python -m compileall -q services/hopvoice/hopvoice services/hopvoice/tests
  python -m py_compile api/services/pipecat/hopvoice_tts.py
  local junit="$RD/pytest-hopvoice.xml" rc=0
  (cd services/hopvoice && python -m pytest -q -rs -p no:cacheprovider --junitxml="$junit") || rc=$?
  # One test is meant to skip without torch (the GPU streaming loop); that
  # one, by name, is the only skip allowed.
  python - "$junit" "$RD/notes.txt" <<'PY' || rc=1
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot()
cases = r.iter("testcase")
skipped = [c for c in r.iter("testcase") if c.find("skipped") is not None]
total = len(list(r.iter("testcase")))
def expected(c):
    # A module-level importorskip is reported with the module as its name.
    where = c.get("classname", "") + "::" + c.get("name", "")
    sk = c.find("skipped")
    why = (sk.get("message") or "") + (sk.text or "")
    return "test_streaming_loop" in where and "torch" in why
other = [c.get("classname", "") + "::" + c.get("name", "") for c in skipped if not expected(c)]
line = f"hopvoice: {total} tests, {len(skipped)} skipped ({len(skipped) - len(other)} expected: torch not installed)"
print(line); open(sys.argv[2], "a").write(line + "\n")
if total == 0: print("no tests ran"); sys.exit(1)
if other: print("unexpected skips:\n  " + "\n  ".join(other)); sys.exit(1)
PY
  return "$rc"
}

do_hopstt() {
  # services/hopstt: install with the test extra into its own venv and run the
  # stub-engine tests (no GPU, no model weights). The client side of the
  # protocol is covered by api/tests/test_hopstt_*.py in the api step.
  if [[ ! -x "$HS_VENV/bin/python" ]]; then "$PY_HOPSTT" -m venv "$HS_VENV"; fi
  # shellcheck disable=SC1091
  source "$HS_VENV/bin/activate"
  python -m pip install -q -e 'services/hopstt[test]'
  python -m compileall -q services/hopstt
  python -m py_compile api/services/pipecat/hopstt_stt.py
  local junit="$RD/pytest-hopstt.xml" rc=0
  (cd services/hopstt && python -m pytest -q -rs -p no:cacheprovider --junitxml="$junit") || rc=$?
  # Only tests that need a GPU, real weights or a real engine may skip
  # (torch/NeMo/vLLM/CUDA, or the opt-in HOPSTT_REAL_MODEL_TESTS suite).
  python - "$junit" "$RD/notes.txt" <<'PY' || rc=1
import re, sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot()
cases = list(r.iter("testcase"))
skipped = [c for c in cases if c.find("skipped") is not None]
gpu = re.compile(r"torch|nemo|vllm|cuda|gpu|HOPSTT_REAL_MODEL_TESTS|real-model", re.I)
def expected(c):
    sk = c.find("skipped")
    return bool(gpu.search((sk.get("message") or "") + (sk.text or "")))
other = [c.get("classname", "") + "::" + c.get("name", "") for c in skipped if not expected(c)]
line = f"hopstt: {len(cases)} tests, {len(skipped)} skipped ({len(skipped) - len(other)} expected: no GPU/real model)"
print(line); open(sys.argv[2], "a").write(line + "\n")
if not cases: print("no tests ran"); sys.exit(1)
if other: print("unexpected skips:\n  " + "\n  ".join(other)); sys.exit(1)
PY
  return "$rc"
}

# The service is built alongside this client; until it has a pyproject the
# step cannot run, which is INCOMPLETE, never a pass.
hopstt_step() {
  if [[ -f services/hopstt/pyproject.toml ]]; then
    step hopstt 900 do_hopstt
  else
    blocked hopstt "services/hopstt/pyproject.toml not found"
  fi
}

export -f do_setup do_drift do_api do_hopvoice do_hopstt
export RD SHA RUN_ID REPO VENV HV_VENV HS_VENV PY PY_HOPVOICE PY_HOPSTT

# ── Profiles ────────────────────────────────────────────────────────────
case "$PROFILE" in
  hopvoice) step hopvoice 900 do_hopvoice ;;
  hopstt) hopstt_step ;;
  drift)
    step --critical setup 1200 do_setup
    step drift 1200 do_drift
    ;;
  api)
    step --critical setup 1200 do_setup
    step api 2400 do_api
    ;;
  fast)
    step --critical setup 1200 do_setup
    step drift 1200 do_drift
    step api 2400 do_api
    step hopvoice 900 do_hopvoice
    hopstt_step
    ;;
esac
exit 0
