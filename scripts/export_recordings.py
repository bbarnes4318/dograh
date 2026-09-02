"""Export workflow run recordings for a date range from a Dograh deployment.

Run from the repo root against any Dograh instance (OSS or hosted). Only the
Python standard library is used, so no virtualenv is required:

    python scripts/export_recordings.py \
        --start 2026-08-25 --end 2026-09-02 \
        --timezone America/New_York \
        --base-url https://your-dograh-host \
        --api-key "$DOGRAH_API_KEY" \
        --out ./recordings

The API key comes from the Developers page (`/api-keys`) in the Dograh UI and is
sent as the `X-API-Key` header, which scopes the export to that key's
organization. `DOGRAH_API_URL` and `DOGRAH_API_KEY` are read as fallbacks for
`--base-url` and `--api-key`.

Dates are interpreted in `--timezone` (UTC by default), so an operator asking
for a range in their own working hours gets the days they mean. The API stores
and filters `created_at` in UTC, so bounds are converted before the request and
run timestamps are converted back for the manifest and file names. Naming a
zone rather than a fixed offset keeps daylight-saving transitions correct: a
range over a US "spring forward" is still whole local days on both sides.

Runs are listed via `GET /api/v1/organizations/usage/runs`, which is scoped to
the API key's organization and bounds `created_at` inclusively on both ends.
Each run can carry up to three recording tracks -- `mixed` (the combined call),
`user`, and `bot`. Audio is fetched through the public download endpoint, which
302-redirects to a short-lived signed storage URL. A run only has such a URL
once it has a public access token; for runs missing one this script calls the
per-run endpoint, which mints the token on demand.

Alongside the audio files, a `manifest.csv` records every run in the range --
including runs with no recording -- so the export can be reconciled against the
usage history in the UI.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PAGE_SIZE = 100
MAX_ATTEMPTS = 4
TRACKS: Tuple[str, ...] = ("mixed", "user", "bot")

# Convenience aliases for zones operators name by abbreviation. Each maps to an
# IANA zone rather than a fixed offset, so "EST" in August correctly resolves to
# EDT instead of silently shifting the range by an hour.
TIMEZONE_ALIASES = {
    "ET": "America/New_York",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CT": "America/Chicago",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MT": "America/Denver",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
}

# Track -> (public URL field, storage key field, artifact type on the public
# download endpoint). The storage key tells us a recording exists even when the
# public URL is absent because the run has no public access token yet.
TRACK_FIELDS: Dict[str, Tuple[str, str, str]] = {
    "mixed": ("recording_public_url", "recording_url", "recording"),
    "user": ("user_recording_public_url", "user_recording_url", "user_recording"),
    "bot": ("bot_recording_public_url", "bot_recording_url", "bot_recording"),
}

CONTENT_TYPE_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}

MANIFEST_COLUMNS = [
    "run_id",
    "workflow_id",
    "workflow_name",
    "run_name",
    "created_at",
    "created_at_local",
    "call_type",
    "mode",
    "caller_number",
    "called_number",
    "disposition",
    "call_duration_seconds",
    "track",
    "downloaded_file",
    "status",
]


class ExportError(Exception):
    """A failure that should stop the export with a readable message."""


def resolve_timezone(name: str) -> ZoneInfo:
    """Look up an IANA zone, accepting the common US abbreviations as aliases."""
    candidate = TIMEZONE_ALIASES.get(name.strip().upper(), name.strip())
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ExportError(
            f"Unknown timezone {name!r}. Use an IANA name such as "
            "America/New_York, or one of: "
            f"{', '.join(sorted(TIMEZONE_ALIASES))}."
        ) from exc


def parse_date_bound(value: str, *, end_of_day: bool, tz: ZoneInfo) -> str:
    """Turn a user-supplied date into the ISO 8601 UTC string the API expects.

    Accepts `YYYY-MM-DD`, the US `M/D/YYYY` shorthand, and full ISO date-times.
    A bare date is read as a whole day in `tz` -- so `--end 2026-09-02` includes
    calls made late on September 2nd local time rather than only midnight -- and
    then converted to UTC, which is what the API filters on. An ISO date-time
    carrying its own offset is respected as given.
    """
    raw = value.strip()

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            day = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if end_of_day:
            day = day + timedelta(days=1) - timedelta(microseconds=1)
        return to_api_timestamp(day.replace(tzinfo=tz))

    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportError(
            f"Could not parse date {value!r}. Use YYYY-MM-DD, M/D/YYYY, "
            "or a full ISO 8601 date-time."
        ) from exc

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return to_api_timestamp(moment)


def to_api_timestamp(moment: datetime) -> str:
    """Render an aware datetime as the UTC ISO 8601 string the API parses."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_local(created_at: Optional[str], tz: ZoneInfo) -> str:
    """Convert a run's UTC `created_at` into `tz` for display and file naming.

    Returns an empty string when the timestamp is missing or unparseable, so a
    surprising value from the API degrades the label rather than the export.
    """
    if not created_at:
        return ""
    try:
        moment = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).isoformat()


def request_json(url: str, api_key: str) -> Any:
    """GET a JSON endpoint, retrying transient network and 5xx failures."""
    request = urllib.request.Request(
        url, headers={"X-API-Key": api_key, "Accept": "application/json"}
    )

    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ExportError(
                    f"Authentication failed ({exc.code}) for {url}. "
                    "Check that the API key is valid and belongs to the "
                    "organization that owns these runs."
                ) from exc
            if exc.code < 500 or attempt == MAX_ATTEMPTS - 1:
                raise ExportError(f"Request to {url} failed: {exc}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise ExportError(f"Request to {url} failed: {exc}") from exc
        time.sleep(2**attempt)

    raise ExportError(f"Request to {url} failed after {MAX_ATTEMPTS} attempts")


def iter_runs(
    base_url: str, api_key: str, start_date: str, end_date: str
) -> Iterator[Dict[str, Any]]:
    """Yield every usage-history run in the date range, page by page."""
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "start_date": start_date,
                "end_date": end_date,
                "page": page,
                "limit": PAGE_SIZE,
            }
        )
        url = f"{base_url}/api/v1/organizations/usage/runs?{query}"
        payload = request_json(url, api_key)

        runs = payload.get("runs") or []
        for run in runs:
            yield run

        total_pages = payload.get("total_pages") or 0
        if page >= total_pages or not runs:
            return
        page += 1


def resolve_public_url(
    run: Dict[str, Any], track: str, base_url: str, api_key: str
) -> Optional[str]:
    """Return a downloadable URL for `track`, minting a token if needed.

    The usage-history listing only exposes public URLs for runs that already
    carry a public access token. The per-run endpoint creates one on demand for
    any run that has an artifact, so fall back to it rather than skipping a
    recording that plainly exists in storage.
    """
    public_field, storage_field, _ = TRACK_FIELDS[track]

    public_url = run.get(public_field)
    if public_url:
        return public_url

    if not run.get(storage_field):
        return None

    workflow_id = run.get("workflow_id")
    run_id = run.get("id")
    if workflow_id is None or run_id is None:
        return None

    detail = request_json(
        f"{base_url}/api/v1/workflow/{workflow_id}/runs/{run_id}", api_key
    )
    return detail.get(public_field)


def choose_extension(url: str, content_type: Optional[str]) -> str:
    """Pick a file extension from the signed URL path, falling back to type."""
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in CONTENT_TYPE_EXTENSIONS.values():
        return suffix

    if content_type:
        base_type = content_type.split(";")[0].strip().lower()
        if base_type in CONTENT_TYPE_EXTENSIONS:
            return CONTENT_TYPE_EXTENSIONS[base_type]

    return ".audio"


def download(url: str, out_dir: Path, stem: str) -> Path:
    """Download a recording, following the public endpoint's signed redirect.

    No API key is sent: the public download endpoint authenticates by token in
    the path, and forwarding credentials on to the storage backend would leak
    them outside the Dograh deployment.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                extension = choose_extension(
                    response.geturl(), response.headers.get("Content-Type")
                )
                destination = out_dir / f"{stem}{extension}"
                partial = destination.with_suffix(destination.suffix + ".part")
                with partial.open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
                partial.replace(destination)
                return destination
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == MAX_ATTEMPTS - 1:
                raise ExportError(f"Download failed ({exc.code}): {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise ExportError(f"Download failed: {exc}") from exc
        time.sleep(2**attempt)

    raise ExportError(f"Download failed after {MAX_ATTEMPTS} attempts: {url}")


def manifest_row(
    run: Dict[str, Any], track: str, file_name: str, status: str, tz: ZoneInfo
):
    return {
        "run_id": run.get("id"),
        "workflow_id": run.get("workflow_id"),
        "workflow_name": run.get("workflow_name"),
        "run_name": run.get("name"),
        "created_at": run.get("created_at"),
        "created_at_local": to_local(run.get("created_at"), tz),
        "call_type": run.get("call_type"),
        "mode": run.get("mode"),
        "caller_number": run.get("caller_number"),
        "called_number": run.get("called_number"),
        "disposition": run.get("disposition"),
        "call_duration_seconds": run.get("call_duration_seconds"),
        "track": track,
        "downloaded_file": file_name,
        "status": status,
    }


def export(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    tz = resolve_timezone(args.timezone)
    start_date = parse_date_bound(args.start, end_of_day=False, tz=tz)
    end_date = parse_date_bound(args.end, end_of_day=True, tz=tz)
    tracks = [track.strip() for track in args.tracks.split(",") if track.strip()]

    unknown = [track for track in tracks if track not in TRACK_FIELDS]
    if unknown:
        raise ExportError(
            f"Unknown track(s): {', '.join(unknown)}. Choose from {', '.join(TRACKS)}."
        )

    out_dir = Path(args.out).expanduser().resolve()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Listing runs from {args.start} to {args.end} ({tz}) "
        f"= {start_date} to {end_date} UTC on {base_url}"
    )

    rows: List[Dict[str, Any]] = []
    run_count = 0
    downloaded = 0
    failed = 0

    for run in iter_runs(
        base_url, api_key=args.api_key, start_date=start_date, end_date=end_date
    ):
        run_count += 1
        found_any = False
        # Name files by the local date so they group under the day the operator
        # asked for, not the UTC day the call may have rolled over into.
        local_day = to_local(run.get("created_at"), tz)[:10]

        for track in tracks:
            try:
                url = resolve_public_url(run, track, base_url, args.api_key)
            except ExportError as exc:
                print(f"  run {run.get('id')} [{track}]: {exc}", file=sys.stderr)
                rows.append(manifest_row(run, track, "", "error", tz))
                failed += 1
                continue

            if not url:
                continue

            found_any = True
            stem = f"{local_day}_run{run.get('id')}_{track}"

            if args.dry_run:
                print(f"  run {run.get('id')} [{track}]: {url}")
                rows.append(manifest_row(run, track, "", "available", tz))
                downloaded += 1
                continue

            try:
                path = download(url, out_dir, stem)
            except ExportError as exc:
                print(f"  run {run.get('id')} [{track}]: {exc}", file=sys.stderr)
                rows.append(manifest_row(run, track, "", "error", tz))
                failed += 1
                continue

            downloaded += 1
            print(f"  run {run.get('id')} [{track}] -> {path.name}")
            rows.append(manifest_row(run, track, path.name, "downloaded", tz))

        if not found_any:
            rows.append(manifest_row(run, "", "", "no_recording", tz))

    if rows and not args.dry_run:
        manifest = out_dir / "manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote manifest: {manifest}")

    verb = "would download" if args.dry_run else "downloaded"
    print(f"{run_count} run(s) in range, {verb} {downloaded} recording(s)")
    if failed:
        print(f"{failed} recording(s) failed -- see 'error' rows", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Dograh workflow run recordings for a date range.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python scripts/export_recordings.py --start 2026-08-25 "
            "--end 2026-09-02 --timezone America/New_York --out ./recordings\n"
        ),
    )
    parser.add_argument(
        "--start", required=True, help="Inclusive start date (YYYY-MM-DD or M/D/YYYY)"
    )
    parser.add_argument(
        "--end", required=True, help="Inclusive end date (YYYY-MM-DD or M/D/YYYY)"
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get("DOGRAH_TIMEZONE", "UTC"),
        help=(
            "IANA timezone the dates are given in, e.g. America/New_York; "
            "US abbreviations like ET/EST are accepted (env: DOGRAH_TIMEZONE, "
            "default: UTC)"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DOGRAH_API_URL", "http://localhost:8000"),
        help="Dograh deployment URL (env: DOGRAH_API_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("DOGRAH_API_KEY"),
        help="Dograh API key sent as X-API-Key (env: DOGRAH_API_KEY)",
    )
    parser.add_argument(
        "--out", default="./recordings", help="Directory to write audio and manifest"
    )
    parser.add_argument(
        "--tracks",
        default="mixed",
        help=f"Comma-separated tracks to export from {TRACKS} (default: mixed)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the recordings that would be exported without downloading",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.api_key:
        print(
            "An API key is required: pass --api-key or set DOGRAH_API_KEY.",
            file=sys.stderr,
        )
        return 2

    try:
        return export(args)
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
