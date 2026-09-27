#!/usr/bin/env bash
# Shared helpers for the hopwhistle cutover scripts. Sourced, not run.
#
# Everything here only ever touches the `api` service of the dograh compose
# project. postgres, redis, minio, ui and hoppwhistle's FreeSWITCH are never
# stopped or recreated (`--no-deps` on every `up`).

set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/dograh}"
OVERRIDE_FILE="$COMPOSE_DIR/docker-compose.override.yaml"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/dograh-backups}"
SRC_DIR="${SRC_DIR:-/opt/dograh-src}"
REPO_URL="${REPO_URL:-https://github.com/bbarnes4318/dograh.git}"
NEW_IMAGE_REPO="${NEW_IMAGE_REPO:-hopwhistle/dograh-api}"
OLD_IMAGE="${OLD_IMAGE:-dograhai/dograh-api:latest}"
# The fork adds exactly one migration on top of what the old image knows.
OLD_ALEMBIC_HEAD="00b0201ad918"
NEW_ALEMBIC_HEAD="b7c41d9e52aa"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-420}"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

compose() { (cd "$COMPOSE_DIR" && docker compose "$@"); }

require_root() { [[ "$(id -u)" == "0" ]] || die "run as root"; }

api_container_id() { compose ps -a -q api | head -n1; }

# DB user/name for psql inside the postgres container. Read from the api
# container's DATABASE_URL (works while api is stopped), falling back to the
# compose defaults (postgres/postgres).
db_user_and_name() {
    local cid url
    cid="$(api_container_id || true)"
    url=""
    if [[ -n "$cid" ]]; then
        url="$(docker inspect "$cid" --format '{{range .Config.Env}}{{println .}}{{end}}' \
            | sed -n 's/^DATABASE_URL=//p' | head -n1)"
    fi
    if [[ "$url" =~ ://([^:/@]+)(:[^@]*)?@[^/]+/([^?]+) ]]; then
        echo "${BASH_REMATCH[1]} ${BASH_REMATCH[3]}"
    else
        echo "postgres postgres"
    fi
}

psql_db() {
    local user name
    read -r user name < <(db_user_and_name)
    compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$user" -d "$name" "$@"
}

alembic_version() {
    psql_db -At -c "SELECT version_num FROM alembic_version" 2>/dev/null | head -n1
}

# Wait until the api container reports healthy. Fails fast if it exits or
# restarts (a migration failure under `set -e` in start_services_docker.sh
# shows up as a restart loop).
wait_api_healthy() {
    local deadline cid status restarts start_restarts
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    cid="$(api_container_id)"
    [[ -n "$cid" ]] || die "no api container"
    start_restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
    log "waiting for api to become healthy (up to ${HEALTH_TIMEOUT_SECONDS}s)..."
    while true; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
        restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
        if [[ "$status" == "healthy" ]]; then
            log "api is healthy"
            return 0
        fi
        if [[ "$restarts" != "$start_restarts" ]] || [[ "$(docker inspect -f '{{.State.Status}}' "$cid")" == "exited" ]]; then
            docker logs --tail 40 "$cid" >&2 || true
            die "api container exited or restarted while starting (status=$status restarts=$restarts)"
        fi
        if (( $(date +%s) > deadline )); then
            docker logs --tail 40 "$cid" >&2 || true
            die "api not healthy after ${HEALTH_TIMEOUT_SECONDS}s (status=$status)"
        fi
        sleep 5
    done
}

# Calls still in progress (started in the last 30 minutes and not completed).
active_calls() {
    psql_db -At -c "SELECT count(*) FROM workflow_runs
        WHERE is_completed IS NOT TRUE
          AND created_at > now() - interval '30 minutes'
          AND mode NOT IN ('textchat')" 2>/dev/null || echo "?"
}

confirm_no_active_calls() {
    local n
    n="$(active_calls)"
    if [[ "$n" != "0" ]]; then
        log "WARNING: $n call(s) look active. Recreating api drops them."
        if [[ "${FORCE:-0}" != "1" ]]; then
            read -r -p "Type YES to continue anyway: " answer
            [[ "$answer" == "YES" ]] || die "aborted"
        fi
    fi
}
