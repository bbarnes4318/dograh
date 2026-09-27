#!/usr/bin/env bash
# Build the dograh-api image from the fork, on this box, without starving
# FreeSWITCH or live transfers.
#
#   sudo bash 1_build_image.sh <git-sha>
#
# - Refuses to start with less than 20 GB free under /var/lib/docker.
# - Builds inside a dedicated BuildKit container pinned to the lower half of
#   the CPU cores. (`docker build --cpuset-cpus` is ignored by BuildKit, which
#   this Dockerfile needs, so the limit is put on the builder container.)
# - Changes nothing that is running: it only adds an image tag.
#
# Output: image hopwhistle/dograh-api:<short-sha>
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

MIN_FREE_GB="${MIN_FREE_GB:-20}"
BUILDER_NAME="${BUILDER_NAME:-hopwhistle-limited}"

require_root
SHA="${1:-}"
[[ -n "$SHA" ]] || die "usage: $0 <git-sha of the fork's main to deploy>"

# ---- disk guard ------------------------------------------------------------
free_kb="$(df -Pk /var/lib/docker | awk 'NR==2 {print $4}')"
free_gb=$(( free_kb / 1024 / 1024 ))
df -h /var/lib/docker
if (( free_gb < MIN_FREE_GB )); then
    die "only ${free_gb} GB free under /var/lib/docker; need ${MIN_FREE_GB} GB. Nothing was built."
fi
log "disk ok: ${free_gb} GB free"

# ---- CPU limit -------------------------------------------------------------
cores="$(nproc)"
half=$(( cores / 2 ))
(( half >= 1 )) || half=1
CPUSET="0-$(( half - 1 ))"
log "building on CPUs ${CPUSET} of ${cores}"

# ---- source ----------------------------------------------------------------
if [[ -d "$SRC_DIR/.git" ]]; then
    git -C "$SRC_DIR" fetch --quiet origin
else
    git clone --quiet "$REPO_URL" "$SRC_DIR"
fi
git -C "$SRC_DIR" checkout --quiet --detach "$SHA"
git -C "$SRC_DIR" submodule update --init --quiet pipecat
SHORT_SHA="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD)"
TAG="$NEW_IMAGE_REPO:$SHORT_SHA"
log "source at $(git -C "$SRC_DIR" log -1 --format='%h %s')"

# ---- builder ---------------------------------------------------------------
# Recreate so a changed core count always takes effect.
docker buildx rm "$BUILDER_NAME" >/dev/null 2>&1 || true
docker buildx create --name "$BUILDER_NAME" --driver docker-container \
    --driver-opt "cpuset-cpus=${CPUSET}" --bootstrap >/dev/null
actual="$(docker inspect "buildx_buildkit_${BUILDER_NAME}0" --format '{{.HostConfig.CpusetCpus}}')"
[[ "$actual" == "$CPUSET" ]] || die "builder not pinned (got '$actual', wanted '$CPUSET')"
log "builder pinned to CPUs $actual"

cleanup_builder() { docker buildx rm "$BUILDER_NAME" >/dev/null 2>&1 || true; }
trap cleanup_builder EXIT

# ---- build -----------------------------------------------------------------
started=$(date +%s)
nice -n 10 docker buildx build \
    --builder "$BUILDER_NAME" \
    --file "$SRC_DIR/api/Dockerfile" \
    --tag "$TAG" \
    --label "org.opencontainers.image.revision=$(git -C "$SRC_DIR" rev-parse HEAD)" \
    --load \
    "$SRC_DIR"
log "built $TAG in $(( ($(date +%s) - started) / 60 )) min"

# ---- sanity: the image carries the expected migration head ------------------
heads="$(docker run --rm --entrypoint sh "$TAG" -c \
    'grep -l "down_revision = \"'"$OLD_ALEMBIC_HEAD"'\"" /app/api/alembic/versions/*.py | xargs -n1 basename')"
[[ "$heads" == "${NEW_ALEMBIC_HEAD}_"* ]] || die "unexpected migration after $OLD_ALEMBIC_HEAD in $TAG: $heads"
log "image migrations ok ($OLD_ALEMBIC_HEAD -> $NEW_ALEMBIC_HEAD)"
echo
echo "NEXT: sudo bash 2_cutover.sh $SHORT_SHA"
