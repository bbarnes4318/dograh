#!/usr/bin/env bash
# Rebuild and restart the UI container from the fork's hopwhistle/customizations
# branch (the source of the box's locally built dograh-ui:voicestudio image).
# Only the `ui` service is recreated; api and calls are not touched.
#
#   sudo bash update_ui.sh
#
# Rolls back to the previous image automatically if the new one is not healthy.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

UI_BRANCH="${UI_BRANCH:-hopwhistle/customizations}"
UI_IMAGE="${UI_IMAGE:-dograh-ui:voicestudio}"
BUILDER_NAME="${BUILDER_NAME:-hopwhistle-limited}"
MIN_FREE_GB="${MIN_FREE_GB:-10}"

require_root
docker image inspect "$UI_IMAGE" >/dev/null 2>&1 \
    || die "image $UI_IMAGE not found on this box. Nothing changed."
grep -rqs "$UI_IMAGE" "$COMPOSE_DIR"/docker-compose*.y*ml \
    || die "$UI_IMAGE is not referenced in $COMPOSE_DIR compose files. Nothing changed."

free_gb=$(( $(df -Pk /var/lib/docker | awk 'NR==2 {print $4}') / 1024 / 1024 ))
(( free_gb >= MIN_FREE_GB )) || die "only ${free_gb} GB free; need ${MIN_FREE_GB}. Nothing changed."

# ---- source ------------------------------------------------------------------
[[ -d "$SRC_DIR/.git" ]] || git clone --quiet "$REPO_URL" "$SRC_DIR"
git -C "$SRC_DIR" fetch --quiet origin "$UI_BRANCH"
git -C "$SRC_DIR" checkout --quiet --detach FETCH_HEAD
SHORT_SHA="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD)"
log "source: $UI_BRANCH @ $(git -C "$SRC_DIR" log -1 --format='%h %s')"
grep -q "send_sms" "$SRC_DIR/ui/src/app/tools/config.tsx" 2>/dev/null \
    && log "Send SMS UI present in source" \
    || log "note: Send SMS UI not found in this branch (fine if you are deploying something else)"

# ---- build (CPU-limited, only adds an image tag) -------------------------------
cores="$(nproc)"; half=$(( cores / 2 )); (( half >= 1 )) || half=1
CPUSET="0-$(( half - 1 ))"
docker buildx rm "$BUILDER_NAME" >/dev/null 2>&1 || true
docker buildx create --name "$BUILDER_NAME" --driver docker-container \
    --driver-opt "cpuset-cpus=${CPUSET}" --bootstrap >/dev/null
trap 'docker buildx rm "$BUILDER_NAME" >/dev/null 2>&1 || true' EXIT

NEW_TAG="dograh-ui:build-$SHORT_SHA"
log "building $NEW_TAG on CPUs $CPUSET (several minutes)"
nice -n 10 docker buildx build --builder "$BUILDER_NAME" \
    --file "$SRC_DIR/ui/Dockerfile" --tag "$NEW_TAG" --load "$SRC_DIR"
docker image inspect "$NEW_TAG" >/dev/null 2>&1 || die "build produced no image"

# ---- switch ------------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
ROLLBACK_TAG="dograh-ui:rollback-$TS"
docker tag "$UI_IMAGE" "$ROLLBACK_TAG"
log "previous UI image saved as $ROLLBACK_TAG"

wait_ui_healthy() {
    local cid deadline status
    cid="$(compose ps -a -q ui | head -n1)"
    [[ -n "$cid" ]] || return 1
    deadline=$(( $(date +%s) + 180 ))
    while (( $(date +%s) < deadline )); do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
        [[ "$status" == "healthy" || "$status" == "running" && -z "$(docker inspect -f '{{if .State.Health}}x{{end}}' "$cid")" ]] && return 0
        [[ "$(docker inspect -f '{{.State.Status}}' "$cid")" == "exited" ]] && return 1
        sleep 5
    done
    return 1
}

docker tag "$NEW_TAG" "$UI_IMAGE"
log "recreating ui on the new image"
compose up -d --no-deps --force-recreate --pull never ui
if wait_ui_healthy; then
    log "UI UPDATE DONE ($SHORT_SHA). Hard-refresh the browser (Ctrl+Shift+R)."
    echo "Rollback, if needed: docker tag $ROLLBACK_TAG $UI_IMAGE && (cd $COMPOSE_DIR && docker compose up -d --no-deps --force-recreate --pull never ui)"
else
    log "new UI not healthy; rolling back"
    docker tag "$ROLLBACK_TAG" "$UI_IMAGE"
    compose up -d --no-deps --force-recreate --pull never ui
    die "UI update failed and was rolled back"
fi
