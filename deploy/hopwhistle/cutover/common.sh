#!/usr/bin/env bash
# Shared helpers for the hopwhistle cutover scripts. Sourced, not run.
#
# Everything here only ever touches the `api` service of the dograh compose
# project. postgres, redis, minio, ui and hoppwhistle's FreeSWITCH are never
# stopped or recreated (`--no-deps` on every `up`).
#
# Decisions (is this safe? what failed? what may run automatically?) are made
# by cutover_plan.py, which is read-only and tested offline. This file only
# carries them out.

set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/dograh}"
OVERRIDE_FILE="$COMPOSE_DIR/docker-compose.override.yaml"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/dograh-backups}"
SRC_DIR="${SRC_DIR:-/opt/dograh-src}"
REPO_URL="${REPO_URL:-https://github.com/bbarnes4318/dograh.git}"
NEW_IMAGE_REPO="${NEW_IMAGE_REPO:-hopwhistle/dograh-api}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-420}"
# Present while a switch is unfinished (survives a killed shell or a reboot).
IN_PROGRESS_LINK="$BACKUP_ROOT/in-progress"
LOCK_FILE="$BACKUP_ROOT/.switch.lock"
CUTOVER_DIR="${CUTOVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

compose() { (cd "$COMPOSE_DIR" && docker compose "$@"); }

require_root() { [[ "$(id -u)" == "0" ]] || die "run as root"; }

api_container_id() { compose ps -a -q api | head -n1; }

# DB user/name for psql inside the postgres container. Read from the api
# container's DATABASE_URL (works while api is stopped), falling back to the
# compose defaults (postgres/postgres). Only the user and database name are
# extracted; the password never leaves docker inspect's output pipe.
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
    compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U "$user" -d "$name" "$@"
}

# The planner runs psql itself (read-only transactions); tell it how.
export_psql_cmd() {
    local user name
    read -r user name < <(db_user_and_name)
    CUTOVER_PSQL_CMD="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
        docker compose exec -T postgres psql -U "$user" -d "$name")"
    export CUTOVER_PSQL_CMD COMPOSE_DIR
}

planner() { python3 "$CUTOVER_DIR/cutover_plan.py" "$@"; }

state_get() {  # state_get <state-dir> <key>
    python3 -c 'import json,sys; v=json.load(open(sys.argv[1]+"/state.json")).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"
}

state_set() {  # state_set <state-dir> <key> <json-value>
    python3 - "$1" "$2" "$3" <<'PY'
import json, os, sys
path = os.path.join(sys.argv[1], "state.json")
with open(path) as f:
    state = json.load(f)
state[sys.argv[2]] = json.loads(sys.argv[3])
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=2)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, path)
PY
}

set_phase() { state_set "$1" phase "\"$2\""; log "phase: $2"; }

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

# healthy | starting | restarting | exited | missing
api_status() {
    local cid st health
    cid="$(api_container_id || true)"
    [[ -n "$cid" ]] || { echo missing; return; }
    st="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo missing)"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$st" in
        running) [[ "$health" == "healthy" ]] && echo healthy || echo starting ;;
        restarting) echo restarting ;;
        exited|dead|created) echo exited ;;
        *) echo missing ;;
    esac
}

# Returns 0 healthy, 10 crashed (exited or restarted), 11 timed out while
# still running. Never stops anything: a container that is still running may
# be in the middle of a long CREATE INDEX CONCURRENTLY.
wait_api_healthy() {
    local deadline cid start_restarts restarts status
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    cid="$(api_container_id)"
    [[ -n "$cid" ]] || return 10
    start_restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
    log "waiting for api to become healthy (up to ${HEALTH_TIMEOUT_SECONDS}s)..."
    while true; do
        status="$(api_status)"
        restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo "$start_restarts")"
        if [[ "$status" == "healthy" ]]; then
            log "api is healthy"
            return 0
        fi
        if [[ "$status" == "exited" || "$status" == "restarting" || "$status" == "missing" \
              || "$restarts" != "$start_restarts" ]]; then
            docker logs --tail 40 "$cid" >&2 || true
            log "api exited or restarted while starting (status=$status)"
            return 10
        fi
        if (( $(date +%s) > deadline )); then
            docker logs --tail 40 "$cid" >&2 || true
            log "api not healthy after ${HEALTH_TIMEOUT_SECONDS}s; it is still running and is left alone"
            return 11
        fi
        sleep 5
    done
}

# ---- switch lock -------------------------------------------------------------

acquire_switch_lock() {  # acquire_switch_lock <state-dir>
    mkdir -p "$BACKUP_ROOT"
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "another cutover/update/recover is running"
    if [[ -e "$IN_PROGRESS_LINK" ]]; then
        die "a previous switch did not finish ($(readlink -f "$IN_PROGRESS_LINK")). Run: sudo bash $CUTOVER_DIR/recover.sh status"
    fi
    ln -sfn "$1" "$IN_PROGRESS_LINK"
}

release_switch_lock() { rm -f "$IN_PROGRESS_LINK"; }

# ---- backups -------------------------------------------------------------------

# pg_dump, the current override and a pinned tag of the running image, into
# the state directory. Nothing running changes.
take_backups() {  # take_backups <state-dir>
    local dir="$1" db_bytes free_bytes prev_id user name
    db_bytes="$(psql_db -At -c "SELECT pg_database_size(current_database())")"
    free_bytes=$(( $(df -Pk "$dir" | awk 'NR==2 {print $4}') * 1024 ))
    # A compressed custom-format dump is far smaller than the live DB; the full
    # size is a safe upper bound.
    (( free_bytes > db_bytes + 2 * 1024 * 1024 * 1024 )) \
        || die "not enough room in $dir for the pg_dump (DB is $(( db_bytes / 1024 / 1024 )) MB). Nothing changed."

    cp -a "$OVERRIDE_FILE" "$dir/docker-compose.override.yaml"
    prev_id="$(state_get "$dir" previous_image_id)"
    local rollback_tag
    rollback_tag="hopwhistle/dograh-api-rollback:$(basename "$dir")"
    docker tag "$prev_id" "$rollback_tag"
    state_set "$dir" rollback_tag "\"$rollback_tag\""

    read -r user name < <(db_user_and_name)
    log "pg_dump $name -> $dir/dograh.dump"
    compose exec -T postgres pg_dump -U "$user" -d "$name" -Fc > "$dir/dograh.dump"
    compose exec -T postgres pg_restore --list < "$dir/dograh.dump" > /dev/null \
        || die "backup at $dir/dograh.dump does not read back; nothing changed"
    log "backup ok: $(du -h "$dir/dograh.dump" | cut -f1)"
    set_phase "$dir" backed_up
}

# Put the pre-switch override back and make sure the image it names is the
# exact image that was running (a pull could have moved the tag).
restore_previous_override() {  # restore_previous_override <state-dir>
    local dir="$1" ref prev_id current rollback_tag
    cp -a "$dir/docker-compose.override.yaml" "$OVERRIDE_FILE"
    prev_id="$(state_get "$dir" previous_image_id)"
    rollback_tag="$(state_get "$dir" rollback_tag)"
    ref="$(cd "$COMPOSE_DIR" && docker compose config --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["api"]["image"])')"
    current="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null || true)"
    if [[ "$current" != "$prev_id" ]]; then
        [[ -n "$rollback_tag" ]] || die "$ref moved and there is no rollback tag"
        log "$ref no longer points at the previous image; re-tagging it"
        docker tag "$rollback_tag" "$ref"
    fi
    log "restored $OVERRIDE_FILE"
}

# ---- the switch ------------------------------------------------------------------

# What a failed switch may do on its own is decided by `cutover_plan.py
# decide`: only "nothing changed", "restore the override file" (api not yet
# recreated) and "start the previous image" (database provably untouched) are
# automatic. Everything else holds and prints the recovery choices.
handle_switch_failure() {  # handle_switch_failure <state-dir>
    local dir="$1" status decision action
    status="$(api_status)"
    if [[ "$status" == "exited" || "$status" == "restarting" ]]; then
        # Not serving; stop it so a restart policy cannot keep re-running a
        # failing migration.
        log "stopping the failed api container ($status)"
        compose stop api || true
    fi
    decision="$(planner decide --state-dir "$dir")" || {
        log "could not evaluate the failure; nothing else is changed. Run: sudo bash $CUTOVER_DIR/recover.sh status"
        return
    }
    printf '%s\n' "$decision" > "$dir/decision.json"
    action="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])' <<<"$decision")"
    log "failure decision: $action"
    case "$action" in
        nothing_changed)
            release_switch_lock
            ;;
        restore_override)
            restore_previous_override "$dir"
            set_phase "$dir" rolled_back
            release_switch_lock
            ;;
        auto_image_rollback)
            restore_previous_override "$dir"
            compose up -d --no-deps --force-recreate --pull never api
            if wait_api_healthy && [[ "$(psql_db -At -c 'SELECT version_num FROM alembic_version')" == "$(state_get "$dir" pre_revision)" ]]; then
                set_phase "$dir" rolled_back
                release_switch_lock
                log "previous image is back; the database was not changed"
            else
                log "THE PREVIOUS IMAGE DID NOT COME BACK HEALTHY. Run: sudo bash $CUTOVER_DIR/recover.sh status"
            fi
            ;;
        *)
            printf '%s\n' "$decision" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("\n" + d["summary"])
for r in d["reasons"]:
    print("  - " + r)
print("\nRecovery choices (sudo bash recover.sh <choice>):")
for o in d["options"]:
    print("  * " + o)'
            log "the switch is held; nothing further is done automatically"
            ;;
    esac
}

# Install the planned override, recreate api, verify. The state directory
# must hold a plan made by `cutover_plan.py plan` with no blockers.
run_switch() {  # run_switch <state-dir>
    local dir="$1" rc
    [[ "$(state_get "$dir" phase)" == "planned" ]] || die "$dir is not a fresh plan"
    [[ -f "$dir/override.new.yaml" ]] || die "$dir has no generated override (the plan is blocked)"
    acquire_switch_lock "$dir"

    SWITCH_DIR="$dir"
    SWITCH_DONE=0
    # shellcheck disable=SC2329  # invoked by the EXIT trap
    on_switch_exit() {
        local code=$?
        [[ "$SWITCH_DONE" == "1" ]] && return 0
        trap - EXIT INT TERM HUP
        log "switch failed (exit $code)"
        handle_switch_failure "$SWITCH_DIR"
        exit 1
    }
    trap on_switch_exit EXIT
    trap 'exit 130' INT TERM HUP

    confirm_no_active_calls
    take_backups "$dir"

    planner fingerprint --state-dir "$dir" \
        || die "the system changed after the plan was made; make a new plan"
    (cd "$COMPOSE_DIR" && docker compose -f docker-compose.yaml -f "$dir/override.new.yaml" config -q) \
        || die "generated override does not validate"

    local tmp
    tmp="$(mktemp "$COMPOSE_DIR/.override.XXXXXX.yaml")"
    cp "$dir/override.new.yaml" "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$OVERRIDE_FILE"   # atomic on the same filesystem
    set_phase "$dir" override_replaced

    set_phase "$dir" recreating
    log "recreating api on $(state_get "$dir" target_image) (applies pending migrations on start)"
    compose up -d --no-deps --force-recreate --pull never api
    rc=0; wait_api_healthy || rc=$?
    (( rc == 0 )) || die "api did not become healthy"
    state_set "$dir" new_image_was_healthy true
    set_phase "$dir" healthy

    planner verify --state-dir "$dir" || die "post-switch verification failed"
    set_phase "$dir" verified
    SWITCH_DONE=1
    trap - EXIT INT TERM HUP
    release_switch_lock
    ln -sfn "$dir" "$BACKUP_ROOT/latest"
    log "SWITCH DONE: api on $(state_get "$dir" target_image), database on $(state_get "$dir" target_head)"
}
