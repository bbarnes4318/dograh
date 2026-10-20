#!/usr/bin/env bash
# Update the api container on a box that is ALREADY on a fork-built image
# (2_cutover.sh was run once). Builds the fork, plans, backs up, switches,
# verifies. Same planner and failure handling as 2_cutover.sh.
#
#   sudo bash update_api.sh                         # deploys origin/main of the fork
#   sudo bash update_api.sh <git-sha> [planner options]
#
# The expected migration head always comes from the built image itself
# (read statically, without starting it), never from a constant.
# Restarting api drops live calls for ~30 s: run it between campaigns.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
[[ -e "$IN_PROGRESS_LINK" ]] \
    && die "a previous switch did not finish. Run: sudo bash $HERE/recover.sh status"
[[ -f "$OVERRIDE_FILE" ]] || die "no $OVERRIDE_FILE"
if grep -q "/opt/dograh-patches/" "$OVERRIDE_FILE"; then
    die "this box still runs the patched stock image. Run 1_build_image.sh and 2_cutover.sh first (see README.md). Nothing changed."
fi
grep -qE "image:[[:space:]]*\"?${NEW_IMAGE_REPO}:" "$OVERRIDE_FILE" \
    || die "$OVERRIDE_FILE has no '${NEW_IMAGE_REPO}:<sha>' image line. Nothing changed."

SHA="${1:-$(git ls-remote "$REPO_URL" refs/heads/main | cut -f1)}"
[[ -n "$SHA" ]] || die "could not resolve the fork's main; pass a git sha"
shift || true
log "deploying $SHA"

# ---- 1. build (adds an image tag only; nothing running changes) ---------------
bash "$HERE/1_build_image.sh" "$SHA"
SHORT_SHA="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD)"
NEW_IMAGE="$NEW_IMAGE_REPO:$SHORT_SHA"

# ---- 2. plan (read-only) -------------------------------------------------------
STATE_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
export_psql_cmd
rc=0
planner plan --state-dir "$STATE_DIR" --target-image "$NEW_IMAGE" --mode update "$@" || rc=$?
(( rc == 0 )) || die "plan is BLOCKED or failed (see above and $STATE_DIR/plan.txt). Nothing changed."
if [[ "${ASSUME_YES:-0}" != "1" ]]; then
    read -r -p "Type UPDATE to apply this plan: " answer
    [[ "$answer" == "UPDATE" ]] || die "aborted; nothing changed"
fi

# ---- 3. switch -------------------------------------------------------------------
run_switch "$STATE_DIR"
echo "Backup and plan: $STATE_DIR"
