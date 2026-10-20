#!/usr/bin/env bash
# Switch the api container from the stock image + bind-mounted patches to the
# fork-built image, with the call hygiene guards OFF.
#
#   sudo bash 2_cutover.sh <short-sha> [planner options]
#
# Planner options are passed to `cutover_plan.py plan`, e.g.
#   --ack-mount <target>=<sha256>   after reviewing `cutover_plan.py diff <target>`
#   --set NAME=VALUE                supply a setting the planner could not derive
#   --allow-migration <rev>         after reviewing a non-additive migration
#
# Order:
#   1. plan (read-only): migration graph read from the image without starting
#      it, the database's complete revision set, every mount checked against
#      mount_manifest.json, the settings that replace patch-only behavior.
#      Any blocker stops here with nothing changed.
#   2. the redacted plan is shown; type CUTOVER to continue (ASSUME_YES=1 skips)
#   3. backups, fingerprint re-check, override replaced atomically, api
#      recreated (pending migrations run on start), verification
# A failure after step 3 starts is handled by handle_switch_failure in
# common.sh: automatic action only when the database is provably untouched.
# Recreating api drops calls in progress: run between campaigns.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
SHORT_SHA="${1:-}"
[[ -n "$SHORT_SHA" ]] || die "usage: $0 <short-sha> [planner options]"
shift
NEW_IMAGE="$NEW_IMAGE_REPO:$SHORT_SHA"

[[ -e "$IN_PROGRESS_LINK" ]] \
    && die "a previous switch did not finish. Run: sudo bash $HERE/recover.sh status"
docker image inspect "$NEW_IMAGE" >/dev/null 2>&1 || die "image $NEW_IMAGE not found; run 1_build_image.sh first"
[[ -f "$OVERRIDE_FILE" ]] || die "no $OVERRIDE_FILE"
grep -q "/opt/dograh-patches/" "$OVERRIDE_FILE" \
    || die "$OVERRIDE_FILE has no patch mounts; already cut over? Use update_api.sh. Nothing changed."

STATE_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
export_psql_cmd

log "planning (read-only) into $STATE_DIR"
rc=0
planner plan --state-dir "$STATE_DIR" --target-image "$NEW_IMAGE" --mode cutover \
    --set CALL_HYGIENE_ENABLED=false "$@" || rc=$?
if (( rc == 3 )); then
    die "plan is BLOCKED (see above and $STATE_DIR/plan.txt). Nothing changed."
elif (( rc != 0 )); then
    die "planning failed. Nothing changed."
fi

if [[ "${ASSUME_YES:-0}" != "1" ]]; then
    read -r -p "Type CUTOVER to apply this plan: " answer
    [[ "$answer" == "CUTOVER" ]] || die "aborted; nothing changed"
fi

run_switch "$STATE_DIR"
echo
echo "Recovery, if you need it later:  sudo bash $HERE/recover.sh status"
echo "Next: run the parity checklist in README.md before any campaign."
