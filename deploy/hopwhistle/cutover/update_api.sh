#!/usr/bin/env bash
# Update the api container on a box that is ALREADY on a fork-built image
# (i.e. 2_cutover.sh was run once). Builds the fork's main, backs up the DB,
# switches the image, lets the api apply any new migrations on start, verifies
# the DB reached the expected head, and rolls back automatically on failure.
#
#   sudo bash update_api.sh            # deploys origin/main of the fork
#   sudo bash update_api.sh <git-sha>  # deploys a specific commit
#
# Restarting api drops live calls for ~30 s: run it between campaigns.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# Head the new code migrates to. Read from the built source when unset.
EXPECTED_HEAD="${EXPECTED_HEAD:-}"

require_root
[[ -f "$OVERRIDE_FILE" ]] || die "no $OVERRIDE_FILE"
# A box still on the stock image has no fork image line. Leftover
# /opt/dograh-patches mounts on a fork image are kept as they are.
grep -qE "image:[[:space:]]*\"?${NEW_IMAGE_REPO}:" "$OVERRIDE_FILE" \
    || die "$OVERRIDE_FILE has no '${NEW_IMAGE_REPO}:<sha>' image line; this box still runs the stock image. Run 1_build_image.sh and 2_cutover.sh first (see README.md). Nothing changed."

SHA="${1:-$(git ls-remote "$REPO_URL" refs/heads/main | cut -f1)}"
[[ -n "$SHA" ]] || die "could not resolve the fork's main; pass a git sha"
log "deploying $SHA"

# ---- 1. build (adds an image tag only; nothing running changes) ---------------
# The build script's own migration sanity check only looks at the first
# migration after the stock image, which still holds.
bash "$HERE/1_build_image.sh" "$SHA"
SHORT_SHA="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD)"
NEW_IMAGE="$NEW_IMAGE_REPO:$SHORT_SHA"
if [[ -z "$EXPECTED_HEAD" ]]; then
    # The head is the one revision no other migration names as its parent.
    versions="$SRC_DIR/api/alembic/versions"
    EXPECTED_HEAD="$(comm -23 \
        <(sed -nE 's/^revision = "([0-9a-f]+)"/\1/p' "$versions"/*.py | sort -u) \
        <(grep -hoE '"[0-9a-f]{12}"' <(grep -h '^down_revision' "$versions"/*.py) | tr -d '"' | sort -u))"
    [[ "$(wc -w <<<"$EXPECTED_HEAD")" == "1" ]] \
        || die "could not find a single migration head (got: ${EXPECTED_HEAD:-none}); set EXPECTED_HEAD"
fi
log "expected migration head: $EXPECTED_HEAD"
docker image inspect "$NEW_IMAGE" >/dev/null 2>&1 || die "image $NEW_IMAGE missing after build"
docker run --rm --entrypoint sh "$NEW_IMAGE" -c "ls /app/api/alembic/versions | grep -q '^${EXPECTED_HEAD}_'" \
    || die "image $NEW_IMAGE does not contain migration $EXPECTED_HEAD; is the fork's main up to date?"

# ---- 2. backups --------------------------------------------------------------
confirm_no_active_calls
PREV_VERSION="$(alembic_version)"
[[ -n "$PREV_VERSION" ]] || die "could not read the DB migration version"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$TS"
mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
cp -a "$OVERRIDE_FILE" "$BACKUP_DIR/docker-compose.override.yaml"
old_cid="$(api_container_id)"
OLD_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$old_cid")"
ROLLBACK_TAG="hopwhistle/dograh-api-rollback:$TS"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_TAG"
read -r DB_USER DB_NAME < <(db_user_and_name)
log "pg_dump $DB_NAME -> $BACKUP_DIR/dograh.dump"
compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$BACKUP_DIR/dograh.dump"
compose exec -T postgres pg_restore --list < "$BACKUP_DIR/dograh.dump" >/dev/null \
    || die "backup does not read back; nothing changed"
log "backup ok: $(du -h "$BACKUP_DIR/dograh.dump" | cut -f1) (DB was on $PREV_VERSION)"

# ---- 3. switch ---------------------------------------------------------------
UPDATE_OK=0
on_exit() {
    local code=$?
    [[ "$UPDATE_OK" == "1" ]] && return 0
    trap - EXIT
    log "update failed (exit $code); rolling back to the previous image"
    cp -a "$BACKUP_DIR/docker-compose.override.yaml" "$OVERRIDE_FILE"
    compose stop api || true
    # The old image refuses to start if the DB points at a migration it does
    # not know. The migrations only add enum values / columns, which old code
    # ignores, so pointing the version back is safe.
    if [[ "$(alembic_version)" != "$PREV_VERSION" ]]; then
        psql_db -c "UPDATE alembic_version SET version_num='$PREV_VERSION'" || true
    fi
    compose up -d --no-deps --force-recreate --pull never api \
        || log "ROLLBACK START FAILED. Override is restored at $OVERRIDE_FILE; backup in $BACKUP_DIR"
    exit 1
}
trap on_exit EXIT

sed -i -E "s#(image:[[:space:]]*\"?)${NEW_IMAGE_REPO}:[A-Za-z0-9._-]+#\1${NEW_IMAGE}#" "$OVERRIDE_FILE"
grep -q "$NEW_IMAGE" "$OVERRIDE_FILE" || die "failed to set image in $OVERRIDE_FILE"
compose config >/dev/null || die "override does not validate"

log "recreating api on $NEW_IMAGE (applies new migrations on start)"
compose up -d --no-deps --force-recreate --pull never api
wait_api_healthy
new_version="$(alembic_version)"
[[ "$new_version" == "$EXPECTED_HEAD" ]] || die "DB on '$new_version', expected $EXPECTED_HEAD"
UPDATE_OK=1

log "UPDATE DONE: api on $NEW_IMAGE, DB on $new_version (was $PREV_VERSION)"
echo "Backup: $BACKUP_DIR"
