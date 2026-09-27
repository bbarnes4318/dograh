#!/usr/bin/env python3
"""Make the campaign insights page and its CSV show real call durations.

Why this exists
---------------
The box's hand-patched `/opt/dograh-patches/reports.py` (mounted over
`api/routes/reports.py`) serves the campaign insights page and its CSV
export. Its `_classify_run` works each call's duration out from the first
and last transcript timestamps, never from the duration the platform
stores in `workflow_runs.usage_info.call_duration_seconds`. A call whose
transcript has fewer than two timestamps shows 0 seconds, and that result
is cached in Redis with the rest of the classification.

This patch selects the stored duration alongside each run and, on every
request, lays it over the (possibly cached) transcript-based one. Past
calls are corrected the moment the page loads -- no cache flush needed.
The transcript span is still used for any call with no stored duration.

Usage (run as root on the box)
------------------------------
    python3 apply_insights_duration_patch.py --dry-run
    python3 apply_insights_duration_patch.py
    # then reload the api (drops calls in progress -- run between campaigns):
    cd /opt/dograh && docker compose restart api
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPORTS = Path("/opt/dograh-patches/reports.py")
MARKER = "stored_duration"

EDITS = [
    (
        "                   r.transcript_url as transcript_url\n"
        "            from workflow_runs r\n",
        "                   r.transcript_url as transcript_url,\n"
        "                   r.usage_info->>'call_duration_seconds' as stored_duration\n"
        "            from workflow_runs r\n",
    ),
    (
        "    # ---- aggregate\n    def _cnt(pred) -> int:\n",
        "    # ---- real call duration\n"
        "    # The classification works duration out from transcript timestamps,\n"
        "    # which is 0 for any transcript with fewer than two of them and is\n"
        "    # cached in Redis. The platform stores the actual call length, so it\n"
        "    # wins whenever it is present; the transcript span stays the fallback.\n"
        "    for r in completed_rows:\n"
        '        c = cached.get(r["id"])\n'
        "        if not c:\n"
        "            continue\n"
        "        try:\n"
        '            stored = float(r["stored_duration"] or 0)\n'
        "        except (TypeError, ValueError):\n"
        "            stored = 0\n"
        "        if stored > 0:\n"
        '            cached[r["id"]] = {**c, "duration": int(round(stored))}\n'
        "\n"
        "    # ---- aggregate\n    def _cnt(pred) -> int:\n",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--reports", type=Path, default=REPORTS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path: Path = args.reports
    if not path.exists():
        print(f"{path} not found")
        return 1
    text = path.read_text()
    if MARKER in text:
        print(f"{path.name}: already patched, nothing to do")
        return 0

    for index, (find, replace) in enumerate(EDITS, start=1):
        count = text.count(find)
        if count != 1:
            print(
                f"NOT APPLYING ANYTHING: anchor for edit {index} found {count} "
                f"times in {path.name}, expected once. Send me the file."
            )
            return 1
        text = text.replace(find, replace, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(text)
    try:
        py_compile.compile(tmp.name, doraise=True)
    except py_compile.PyCompileError as e:
        print(f"NOT APPLYING ANYTHING: patched file does not compile:\n{e}")
        return 1
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if args.dry_run:
        print(f"{path.name}: would apply {len(EDITS)} edit(s); patched file compiles")
        print("Dry run -- nothing written.")
        return 0

    backup = path.with_name(
        f"{path.name}.bak-insights-duration-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(path, backup)
    path.write_text(text)
    print(f"{path.name}: applied {len(EDITS)} edit(s)  (backup: {backup.name})")
    print("Now reload the api:  cd /opt/dograh && docker compose restart api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
