#!/usr/bin/env bash
# Explicit recovery choices after a cutover/update that did not finish.
#
#   sudo bash recover.sh [--state-dir DIR] status
#   sudo bash recover.sh [--state-dir DIR] hold
#   sudo bash recover.sh [--state-dir DIR] resume-forward
#   sudo bash recover.sh [--state-dir DIR] image-rollback [--stamp-old-head]
#   sudo bash recover.sh [--state-dir DIR] restore-db
#   sudo bash recover.sh [--state-dir DIR] release
#
# DIR defaults to the unfinished switch (/opt/dograh-backups/in-progress),
# else the newest one (latest).
#
# Every choice checks its own preconditions with `cutover_plan.py check`
# (read-only) before changing anything:
#
#   status          read-only: database revision(s), container state, invalid
#                   indexes, rows using new enum values, and which choices are
#                   currently allowed.
#   hold            stop api and leave everything else as it is.
#   resume-forward  start the NEW image again (it understands every revision
#                   in its graph and re-runs remaining idempotent migrations).
#                   Refused while invalid indexes exist or the DB revision is
#                   unknown to it.
#   image-rollback  start the PREVIOUS image. Allowed without flags only when
#                   the database is provably where that image left it.
#     --stamp-old-head  run the previous image on the forward schema: only
#                   alembic_version is set back (the additive schema stays).
#                   Refused unless every forward migration is additive or an
#                   enum value addition, no invalid indexes exist and no row
#                   uses a new enum value. Asks for typed confirmation.
#   restore-db      prints the manual database-restore procedure; runs nothing.
#   release         clear the unfinished-switch marker after you resolved it
#                   by hand. Asks for typed confirmation.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
STATE_DIR=""
if [[ "${1:-}" == "--state-dir" ]]; then
    STATE_DIR="$2"
    shift 2
fi
CHOICE="${1:-status}"
shift || true
if [[ -z "$STATE_DIR" ]]; then
    if [[ -e "$IN_PROGRESS_LINK" ]]; then STATE_DIR="$IN_PROGRESS_LINK"; else STATE_DIR="$BACKUP_ROOT/latest"; fi
fi
STATE_DIR="$(readlink -f "$STATE_DIR")"
[[ -f "$STATE_DIR/state.json" ]] || die "$STATE_DIR is not a cutover state directory"
export_psql_cmd

exec 9>"$LOCK_FILE"
flock -n 9 || die "another cutover/update/recover is running"

check() { planner check --state-dir "$STATE_DIR" "$1"; }

start_and_verify_previous() {
    restore_previous_override "$STATE_DIR"
    compose up -d --no-deps --force-recreate --pull never api
    wait_api_healthy || die "previous image did not become healthy; run: sudo bash $0 status"
    local running prev
    running="$(docker inspect -f '{{.Image}}' "$(api_container_id)")"
    prev="$(state_get "$STATE_DIR" previous_image_id)"
    [[ "$running" == "$prev" ]] || die "api is running $running, expected the previous image $prev"
    set_phase "$STATE_DIR" rolled_back
    release_switch_lock
    log "PREVIOUS IMAGE RUNNING with the previous override"
}

case "$CHOICE" in
    status)
        log "state: $STATE_DIR (phase $(state_get "$STATE_DIR" phase))"
        [[ -f "$STATE_DIR/plan.txt" ]] && sed 's/^/    /' "$STATE_DIR/plan.txt"
        planner decide --state-dir "$STATE_DIR"
        ;;
    hold)
        compose stop api
        log "api stopped; nothing else changed"
        ;;
    resume-forward)
        check resume-forward || die "resume-forward is not allowed (see above)"
        [[ -f "$STATE_DIR/override.new.yaml" ]] || die "no planned override in $STATE_DIR"
        if ! cmp -s "$STATE_DIR/override.new.yaml" "$OVERRIDE_FILE"; then
            cp "$STATE_DIR/override.new.yaml" "$OVERRIDE_FILE"
            log "installed the planned override"
        fi
        compose up -d --no-deps --force-recreate --pull never api
        wait_api_healthy || die "new image did not become healthy; run: sudo bash $0 status"
        state_set "$STATE_DIR" new_image_was_healthy true
        planner verify --state-dir "$STATE_DIR" || die "verification failed; the new image is left running"
        set_phase "$STATE_DIR" verified
        release_switch_lock
        ln -sfn "$STATE_DIR" "$BACKUP_ROOT/latest"
        log "NEW IMAGE RUNNING and verified"
        ;;
    image-rollback)
        if check image-rollback; then
            start_and_verify_previous
        elif [[ "${1:-}" == "--stamp-old-head" ]]; then
            check stamp-old-head || die "stamping alembic_version back is not allowed (see above)"
            pre="$(state_get "$STATE_DIR" pre_revision)"
            current="$(psql_db -At -c 'SELECT version_num FROM alembic_version')"
            [[ "$(grep -c . <<<"$current")" == "1" ]] || die "alembic_version is not a single row"
            echo "This sets alembic_version from $current back to $pre. The schema added by the"
            echo "forward migrations stays in place; the previous code ignores it."
            read -r -p "Type STAMP $pre to continue: " answer
            [[ "$answer" == "STAMP $pre" ]] || die "aborted; nothing changed"
            compose stop api
            psql_db -c "UPDATE alembic_version SET version_num = '$pre' WHERE version_num = '$current'"
            state_set "$STATE_DIR" stamped_back_from "\"$current\""
            start_and_verify_previous
            log "To move forward again later, plan and switch again (2_cutover.sh or update_api.sh, whichever matches the restored override); the planner re-checks the migrations."
        else
            die "the previous image cannot start against this database as it is (see above). Consider --stamp-old-head, resume-forward or hold."
        fi
        ;;
    restore-db)
        cat <<EOF
Database restore is manual and destroys everything written since the backup
($(stat -c %y "$STATE_DIR/dograh.dump" 2>/dev/null || echo 'backup time unknown')).
Use it only when neither resume-forward nor image-rollback is acceptable.

  1. sudo bash $0 --state-dir $STATE_DIR hold
  2. Restore into a SCRATCH database first and check it:
       docker compose exec -T postgres createdb -U <user> dograh_restore_check
       docker compose exec -T postgres pg_restore -U <user> -d dograh_restore_check < $STATE_DIR/dograh.dump
       docker compose exec -T postgres psql -U <user> -d dograh_restore_check -c 'SELECT version_num FROM alembic_version'
     It must print $(state_get "$STATE_DIR" pre_revision).
  3. Only then, with api stopped:
       docker compose exec -T postgres pg_restore -U <user> -d <db> --clean --if-exists < $STATE_DIR/dograh.dump
  4. sudo bash $0 --state-dir $STATE_DIR image-rollback
EOF
        ;;
    release)
        read -r -p "Type RELEASE if you resolved this switch by hand: " answer
        [[ "$answer" == "RELEASE" ]] || die "aborted"
        set_phase "$STATE_DIR" released_by_operator
        release_switch_lock
        log "unfinished-switch marker cleared"
        ;;
    *)
        die "unknown choice '$CHOICE' (status | hold | resume-forward | image-rollback | restore-db | release)"
        ;;
esac
