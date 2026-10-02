#!/usr/bin/env bash
# Read-only morning-after report for the call hygiene guards.
#
#   sudo bash day_one_review.sh 2026-10-01 [baseline-day] [ui-base-url]
#
# Prints to the screen and saves a copy under /opt/dograh-backups/reviews/.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REVIEW_DAY="${1:-$(date -d yesterday +%F)}"
BASELINE_DAY="${2:-2026-09-25}"
UI_BASE="${3:-${UI_BASE:-https://YOUR-DOGRAH-UI-HOST}}"
TZ_NAME="${REVIEW_TZ:-America/New_York}"

[[ "$REVIEW_DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "day must be YYYY-MM-DD"
[[ "$BASELINE_DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "baseline must be YYYY-MM-DD"

mkdir -p "$BACKUP_ROOT/reviews"
OUT="$BACKUP_ROOT/reviews/day-one-$REVIEW_DAY.txt"

psql_db -P pager=off \
    -v review_day="$REVIEW_DAY" -v baseline_day="$BASELINE_DAY" \
    -v tz="$TZ_NAME" -v ui_base="$UI_BASE" \
    < "$HERE/day_one_review.sql" | tee "$OUT"

echo
echo "Saved to $OUT"
