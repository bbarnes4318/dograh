#!/usr/bin/env bash
# Switch the api container from the stock image + 20 bind mounts to the
# fork-built image, with the call hygiene guards OFF.
#
#   sudo bash 2_cutover.sh <short-sha printed by 1_build_image.sh>
#
# Order:
#   1. preflight (image present, DB on the old migration, box settings found)
#   2. backups, timestamped: pg_dump of the dograh DB, the current override,
#      and a pinned tag of the current api image
#   3. write the new override (validated with `docker compose config`)
#   4. recreate ONLY the api container, wait for healthy
#   5. confirm the DB is on the new migration
# If step 4 or 5 fails, rollback.sh runs automatically (NO_AUTO_ROLLBACK=1
# to skip). Recreating api drops calls in progress: run between campaigns.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
SHORT_SHA="${1:-}"
[[ -n "$SHORT_SHA" ]] || die "usage: $0 <short-sha>"
NEW_IMAGE="$NEW_IMAGE_REPO:$SHORT_SHA"

# ---- 1. preflight ----------------------------------------------------------
docker image inspect "$NEW_IMAGE" >/dev/null 2>&1 || die "image $NEW_IMAGE not found; run 1_build_image.sh first"
[[ -f "$OVERRIDE_FILE" ]] || die "no $OVERRIDE_FILE"
grep -q "/opt/dograh-patches/" "$OVERRIDE_FILE" \
    || die "$OVERRIDE_FILE has no patch mounts; already cut over? Nothing changed."

current_version="$(alembic_version)"
[[ "$current_version" == "$OLD_ALEMBIC_HEAD" ]] \
    || die "DB is on migration '$current_version', expected $OLD_ALEMBIC_HEAD. Nothing changed."

# Box-specific values live in the mounted files, not in the public repo.
ARI_PROVIDER_SRC="$(sed -n 's#^\s*- \(/opt/[^:]*\):/app/api/services/telephony/providers/ari/provider.py.*#\1#p' "$OVERRIDE_FILE" | head -n1)"
DISPATCHER_SRC="$(sed -n 's#^\s*- \(/opt/[^:]*\):/app/api/services/campaign/campaign_call_dispatcher.py.*#\1#p' "$OVERRIDE_FILE" | head -n1)"
TRUNK="${ARI_PJSIP_DEFAULT_TRUNK:-$(grep -oE 'PJSIP/\{to_number\}@[A-Za-z0-9_.-]+' "$ARI_PROVIDER_SRC" | head -n1 | sed 's/.*@//')}"
TRANSFER_CID="${ARI_TRANSFER_DEFAULT_CALLER_ID:-$(grep -oE 'kwargs.get\("caller_id"\) or "\+[0-9]+"' "$ARI_PROVIDER_SRC" | grep -oE '\+[0-9]+' | head -n1)}"
TRANSFER_DEST="${CAMPAIGN_DEFAULT_TRANSFER_DESTINATION:-$(grep -A3 '"transfer_destination": (' "$DISPATCHER_SRC" | grep -oE '"\+[0-9]+"' | tr -d '"' | head -n1)}"
[[ -n "$TRUNK" && -n "$TRANSFER_CID" && -n "$TRANSFER_DEST" ]] \
    || die "could not read trunk/caller ID/transfer destination from the mounted files; set ARI_PJSIP_DEFAULT_TRUNK, ARI_TRANSFER_DEFAULT_CALLER_ID and CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"
log "box settings: trunk=$TRUNK transfer_caller_id=$TRANSFER_CID default_transfer_destination=$TRANSFER_DEST"

db_bytes="$(psql_db -At -c "SELECT pg_database_size(current_database())")"
mkdir -p "$BACKUP_ROOT"
free_bytes=$(( $(df -Pk "$BACKUP_ROOT" | awk 'NR==2 {print $4}') * 1024 ))
# A compressed custom-format dump is far smaller than the live DB; the full
# size is a safe upper bound.
(( free_bytes > db_bytes + 2 * 1024 * 1024 * 1024 )) \
    || die "not enough room in $BACKUP_ROOT for the pg_dump (DB is $(( db_bytes / 1024 / 1024 )) MB). Nothing changed."

confirm_no_active_calls

# ---- 2. backups ------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$TS"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cp -a "$OVERRIDE_FILE" "$BACKUP_DIR/docker-compose.override.yaml"

old_cid="$(api_container_id)"
old_image_id="$(docker inspect -f '{{.Image}}' "$old_cid")"
ROLLBACK_TAG="hopwhistle/dograh-api-rollback:$TS"
docker tag "$old_image_id" "$ROLLBACK_TAG"

read -r DB_USER DB_NAME < <(db_user_and_name)
log "pg_dump $DB_NAME -> $BACKUP_DIR/dograh.dump"
compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$BACKUP_DIR/dograh.dump"
compose exec -T postgres pg_restore --list < "$BACKUP_DIR/dograh.dump" > /dev/null \
    || die "backup at $BACKUP_DIR/dograh.dump does not read back; nothing changed"
log "backup ok: $(du -h "$BACKUP_DIR/dograh.dump" | cut -f1)"

cat > "$BACKUP_DIR/rollback.env" <<EOF
OLD_IMAGE_ID=$old_image_id
ROLLBACK_TAG=$ROLLBACK_TAG
OLD_ALEMBIC_HEAD=$OLD_ALEMBIC_HEAD
NEW_IMAGE=$NEW_IMAGE
EOF
ln -sfn "$BACKUP_DIR" "$BACKUP_ROOT/latest"
log "backups in $BACKUP_DIR"

# ---- 3. new override -------------------------------------------------------
tmp_override="$(mktemp "$COMPOSE_DIR/.override.XXXXXX.yaml")"
python3 "$HERE/gen_override.py" "$OVERRIDE_FILE" "$NEW_IMAGE" "$tmp_override" \
    CALL_HYGIENE_ENABLED=false \
    "ARI_PJSIP_DEFAULT_TRUNK=$TRUNK" \
    "ARI_TRANSFER_DEFAULT_CALLER_ID=$TRANSFER_CID" \
    "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION=$TRANSFER_DEST"
(cd "$COMPOSE_DIR" && docker compose -f docker-compose.yaml -f "$tmp_override" config -q) \
    || { rm -f "$tmp_override"; die "generated override does not validate; nothing changed"; }
mv "$tmp_override" "$OVERRIDE_FILE"
chmod 644 "$OVERRIDE_FILE"
log "new override written:"
sed 's/^/    /' "$OVERRIDE_FILE"

# ---- 4/5. switch -----------------------------------------------------------
# An EXIT trap, not ERR: `die` exits explicitly, which an ERR trap never sees.
CUTOVER_OK=0
on_exit() {
    local code=$?
    [[ "$CUTOVER_OK" == "1" ]] && return 0
    trap - EXIT
    log "cutover failed (exit $code)"
    if [[ "${NO_AUTO_ROLLBACK:-0}" != "1" ]]; then
        log "rolling back automatically"
        FORCE=1 bash "$HERE/rollback.sh" "$BACKUP_DIR" || log "AUTOMATIC ROLLBACK FAILED; run: sudo bash $HERE/rollback.sh $BACKUP_DIR"
    else
        log "run: sudo bash $HERE/rollback.sh $BACKUP_DIR"
    fi
    exit 1
}
trap on_exit EXIT

log "recreating api on $NEW_IMAGE (runs the one new migration on start)"
compose up -d --no-deps --force-recreate --pull never api
wait_api_healthy

new_version="$(alembic_version)"
[[ "$new_version" == "$NEW_ALEMBIC_HEAD" ]] || die "DB on '$new_version', expected $NEW_ALEMBIC_HEAD"
CUTOVER_OK=1

log "CUTOVER DONE: api on $NEW_IMAGE, DB on $new_version, call hygiene guards OFF"
echo
echo "Rollback, if you need it:  sudo bash $HERE/rollback.sh $BACKUP_DIR"
echo "Next: run the parity checklist in README.md before any campaign."
