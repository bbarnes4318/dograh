#!/usr/bin/env bash
# Put api back on the image and override from before a cutover/update.
#
#   sudo bash rollback.sh [state-dir]      # default: the unfinished switch, else latest
#
# This is `recover.sh image-rollback`. It no longer rewrites alembic_version on
# its own: when the database has moved past what the previous image knows, it
# refuses and points at the explicit choices (see recover.sh and README.md).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${1:-}" ]]; then
    exec bash "$HERE/recover.sh" --state-dir "$1" image-rollback
fi
exec bash "$HERE/recover.sh" image-rollback
