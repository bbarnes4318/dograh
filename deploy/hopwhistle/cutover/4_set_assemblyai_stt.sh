#!/usr/bin/env bash
# AssemblyAI platform STT: provision the key, run the smoke test, move the
# rollout, set budgets. See ../assemblyai/README.md.
#
#   sudo bash 4_set_assemblyai_stt.sh key                        # prompts; never echoed
#   sudo bash 4_set_assemblyai_stt.sh smoke [--wav FILE]          # one real session, no restart
#   sudo bash 4_set_assemblyai_stt.sh report                      # who the default reaches (read-only)
#   sudo bash 4_set_assemblyai_stt.sh rollout off
#   sudo bash 4_set_assemblyai_stt.sh rollout canary --orgs 3,7 [--workflows 42]
#   sudo bash 4_set_assemblyai_stt.sh rollout global
#   sudo bash 4_set_assemblyai_stt.sh budget --daily 5 --total 25 --streams 5
#   sudo bash 4_set_assemblyai_stt.sh fallback deepgram|none
#
# Changes edit docker-compose.override.yaml (backup kept next to it) and
# recreate ONLY the api container, which drops calls in progress: run between
# campaigns (FORCE=1 skips the active-call prompt). `smoke` and `report`
# change nothing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
[[ -f "$OVERRIDE_FILE" ]] || die "no $OVERRIDE_FILE"

apply_env() {  # apply_env <json> <label>
    local json="$1" label="$2" ts
    confirm_no_active_calls
    ts="$(date +%Y%m%d-%H%M%S)"
    cp -a "$OVERRIDE_FILE" "$OVERRIDE_FILE.bak-assemblyai-$ts"
    chmod 600 "$OVERRIDE_FILE.bak-assemblyai-$ts"
    printf '%s' "$json" | python3 "$HERE/set_override_env.py" "$OVERRIDE_FILE" \
        || die "override not changed"
    (cd "$COMPOSE_DIR" && docker compose config -q) || {
        cp -a "$OVERRIDE_FILE.bak-assemblyai-$ts" "$OVERRIDE_FILE"
        die "override did not validate; restored it"
    }
    log "$label (previous override: $OVERRIDE_FILE.bak-assemblyai-$ts)"
    compose up -d --no-deps --force-recreate --pull never api
    wait_api_healthy || die "api did not become healthy; previous override: $OVERRIDE_FILE.bak-assemblyai-$ts"
}

json_of() {  # json_of NAME VALUE [NAME VALUE ...]
    python3 -c 'import json,sys; a=sys.argv[1:]; print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"
}

cmd="${1:-}"; shift || true
case "$cmd" in
    key)
        read -rs -p "AssemblyAI API key (input hidden): " KEY; echo
        [[ ${#KEY} -ge 16 ]] || die "that does not look like an API key; nothing changed"
        JSON="$(KEY="$KEY" python3 -c 'import json,os; print(json.dumps({"ASSEMBLYAI_API_KEY": os.environ["KEY"]}))')"
        unset KEY
        apply_env "$JSON" "ASSEMBLYAI_API_KEY set"
        unset JSON
        compose exec -T api python -c 'import os,sys; sys.exit(0 if os.environ.get("ASSEMBLYAI_API_KEY") else 1)' \
            || die "api does not see ASSEMBLYAI_API_KEY"
        log "api sees ASSEMBLYAI_API_KEY (value not shown). Next: $0 smoke"
        ;;
    smoke)
        compose exec -T api python -m api.cli.assemblyai_smoke "$@"
        ;;
    report)
        compose exec -T api python -m api.cli.assemblyai_migrate --report
        ;;
    rollout)
        mode="${1:-}"; shift || true
        orgs=""; workflows=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --orgs) orgs="$2"; shift 2 ;;
                --workflows) workflows="$2"; shift 2 ;;
                *) die "unknown option $1" ;;
            esac
        done
        case "$mode" in
            off|global) ;;
            canary) [[ -n "$orgs$workflows" ]] || die "canary needs --orgs and/or --workflows" ;;
            *) die "usage: $0 rollout off|canary|global" ;;
        esac
        [[ "$orgs$workflows" =~ ^[0-9,]*$ ]] || die "ids must be numbers separated by commas"
        if [[ "$mode" != "off" ]]; then
            compose exec -T api python -c 'import os,sys; sys.exit(0 if os.environ.get("ASSEMBLYAI_API_KEY") else 1)' \
                || die "ASSEMBLYAI_API_KEY is not provisioned: run '$0 key' and '$0 smoke' first"
            compose exec -T api python -m api.cli.assemblyai_smoke >/dev/null \
                || die "smoke test failed; rollout NOT changed (run '$0 smoke' to see why)"
        fi
        apply_env "$(json_of ASSEMBLYAI_STT_ROLLOUT "$mode" \
            ASSEMBLYAI_STT_CANARY_ORG_IDS "$orgs" ASSEMBLYAI_STT_CANARY_WORKFLOW_IDS "$workflows")" \
            "ASSEMBLYAI_STT_ROLLOUT=$mode orgs=[$orgs] workflows=[$workflows]"
        ;;
    budget)
        daily=""; total=""; streams=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --daily) daily="$2"; shift 2 ;;
                --total) total="$2"; shift 2 ;;
                --streams) streams="$2"; shift 2 ;;
                *) die "unknown option $1" ;;
            esac
        done
        [[ -n "$daily$total$streams" ]] || die "give --daily, --total and/or --streams"
        args=()
        [[ -n "$daily" ]] && args+=(ASSEMBLYAI_DAILY_BUDGET_USD "$daily")
        [[ -n "$total" ]] && args+=(ASSEMBLYAI_TOTAL_BUDGET_USD "$total")
        [[ -n "$streams" ]] && args+=(ASSEMBLYAI_MAX_CONCURRENT_STREAMS "$streams")
        apply_env "$(json_of "${args[@]}")" "budgets: daily=$daily total=$total streams=$streams"
        ;;
    fallback)
        provider="${1:-}"
        [[ -n "$provider" ]] || die "usage: $0 fallback <provider>|none"
        [[ "$provider" == "none" ]] && provider=""
        apply_env "$(json_of ASSEMBLYAI_FALLBACK_PROVIDER "$provider")" \
            "ASSEMBLYAI_FALLBACK_PROVIDER=${provider:-<none>}"
        ;;
    *)
        sed -n '2,18p' "$0"; exit 2 ;;
esac
