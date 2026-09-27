#!/usr/bin/env bash
# Put the api back on the stock image + the 20 bind mounts.
#
#   sudo bash rollback.sh [backup-dir]      # default: /opt/dograh-backups/latest
#
# Steps:
#   1. stop api
#   2. point alembic_version back at 00b0201ad918 — the old image runs
#      `alembic upgrade head` under `set -e` on start and crash-loops on a
#      revision it cannot find. The nullable workflow_runs.transcript_text
#      column the new migration added is left in place; the old code ignores it.
#   3. restore the backed-up docker-compose.override.yaml
#   4. make sure the old image tag still points at the exact image that was
#      running before the cutover (a `docker compose pull` since then could
#      have moved `latest`), and start api without pulling
#   5. wait for healthy and confirm the migration version
#
# Does not restore the pg_dump: no data is lost by rolling back. The dump is
# for disasters only (see README.md).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
BACKUP_DIR="${1:-$BACKUP_ROOT/latest}"
BACKUP_DIR="$(readlink -f "$BACKUP_DIR")"
[[ -f "$BACKUP_DIR/docker-compose.override.yaml" && -f "$BACKUP_DIR/rollback.env" ]] \
    || die "$BACKUP_DIR is not a cutover backup"
# shellcheck disable=SC1091
. "$BACKUP_DIR/rollback.env"
docker image inspect "$ROLLBACK_TAG" >/dev/null 2>&1 || die "rollback image $ROLLBACK_TAG is missing"
log "rolling back using $BACKUP_DIR"

# ---- 1. stop api -----------------------------------------------------------
log "stopping api"
compose stop api

# ---- 2. migration pointer --------------------------------------------------
version="$(alembic_version)"
if [[ "$version" != "$OLD_ALEMBIC_HEAD" ]]; then
    log "alembic_version is '$version'; setting it to $OLD_ALEMBIC_HEAD"
    psql_db -c "UPDATE alembic_version SET version_num = '$OLD_ALEMBIC_HEAD'"
fi
[[ "$(alembic_version)" == "$OLD_ALEMBIC_HEAD" ]] || die "could not reset alembic_version"

# ---- 3. override -----------------------------------------------------------
cp -a "$BACKUP_DIR/docker-compose.override.yaml" "$OVERRIDE_FILE"
log "restored $OVERRIDE_FILE"

# ---- 4. image --------------------------------------------------------------
api_image_ref="$(cd "$COMPOSE_DIR" && docker compose config --format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["api"]["image"])')"
current_id="$(docker image inspect -f '{{.Id}}' "$api_image_ref" 2>/dev/null || true)"
if [[ "$current_id" != "$OLD_IMAGE_ID" ]]; then
    log "$api_image_ref no longer points at the pre-cutover image; re-tagging it"
    docker tag "$ROLLBACK_TAG" "$api_image_ref"
fi

log "starting api on $api_image_ref"
compose up -d --no-deps --force-recreate --pull never api

# ---- 5. verify -------------------------------------------------------------
wait_api_healthy
final_version="$(alembic_version)"
[[ "$final_version" == "$OLD_ALEMBIC_HEAD" ]] || die "DB on '$final_version' after rollback"
running_image="$(docker inspect -f '{{.Image}}' "$(api_container_id)")"
[[ "$running_image" == "$OLD_IMAGE_ID" ]] || die "api is running $running_image, expected $OLD_IMAGE_ID"
log "ROLLBACK DONE: api on the pre-cutover image with all mounts, DB on $final_version"
