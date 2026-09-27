#!/usr/bin/env bash
# Turn the call hygiene guards on or off for every workflow.
#
#   sudo bash 3_set_call_hygiene.sh on     # after the parity day
#   sudo bash 3_set_call_hygiene.sh off    # kill switch
#
# Flips CALL_HYGIENE_ENABLED in docker-compose.override.yaml and recreates
# ONLY the api container (same image, no pull, no migration). Recreating api
# drops calls in progress: run between campaigns. The TTS markup scrub stays
# on either way.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

require_root
case "${1:-}" in
    on) VALUE=true ;;
    off) VALUE=false ;;
    *) die "usage: $0 on|off" ;;
esac

grep -qE '^\s+CALL_HYGIENE_ENABLED:' "$OVERRIDE_FILE" \
    || die "CALL_HYGIENE_ENABLED is not in $OVERRIDE_FILE; run 2_cutover.sh first"

confirm_no_active_calls

TS="$(date +%Y%m%d-%H%M%S)"
cp -a "$OVERRIDE_FILE" "$OVERRIDE_FILE.bak-hygiene-$TS"
sed -i -E "s/^(\s+CALL_HYGIENE_ENABLED:).*/\1 \"$VALUE\"/" "$OVERRIDE_FILE"
(cd "$COMPOSE_DIR" && docker compose config -q) || {
    cp -a "$OVERRIDE_FILE.bak-hygiene-$TS" "$OVERRIDE_FILE"
    die "override did not validate; restored it"
}
log "CALL_HYGIENE_ENABLED=$VALUE (previous override: $OVERRIDE_FILE.bak-hygiene-$TS)"

compose up -d --no-deps --force-recreate --pull never api
wait_api_healthy

actual="$(compose exec -T api printenv CALL_HYGIENE_ENABLED)"
[[ "$actual" == "$VALUE" ]] || die "api sees CALL_HYGIENE_ENABLED='$actual'"
log "DONE: call hygiene guards $1"
