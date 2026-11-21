"""Download pinned model revisions into the cache volume and verify them.

    python -m hopstt.tools.fetch_models                 # HOPSTT_ALLOWED_MODELS
    python -m hopstt.tools.fetch_models --models parakeet-tdt-0.6b-v3 whisper-base
    python -m hopstt.tools.fetch_models --verify-only

Files land in ``HOPSTT_MODEL_CACHE`` (Hugging Face cache layout) at the
revision pinned in ``hopstt/models.py`` (override with
``HOPSTT_MODEL_REVISIONS=id=<commit>``). After download every expected file
must exist and every sha256 Hugging Face publishes for an LFS weight file must
match; exit status 1 otherwise. Set ``HF_TOKEN`` for gated repos.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..config import Settings
from ..models import CATALOG
from ..weights import WeightsMissing, snapshot_dir, verify_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="*", help="model ids (default: HOPSTT_ALLOWED_MODELS)")
    parser.add_argument("--cache", help="override HOPSTT_MODEL_CACHE")
    parser.add_argument("--verify-only", action="store_true", help="do not download, only verify")
    parser.add_argument("--skip-sha", action="store_true", help="skip sha256 verification (faster)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.cache:
        settings = settings.replace(model_cache=args.cache)
    model_ids = args.models or list(settings.allowed_models)
    report = []
    ok = True
    for model_id in model_ids:
        if model_id not in CATALOG:
            print(f"unknown model id {model_id!r}; known: {sorted(CATALOG)}", file=sys.stderr)
            return 2
        spec = settings.spec(model_id)
        if not spec.hf_repo or not spec.files:
            report.append({"model": model_id, "status": "skipped", "reason": "no local weights"})
            continue
        try:
            root = snapshot_dir(spec, settings, download=not args.verify_only)
        except WeightsMissing as exc:
            ok = False
            report.append({"model": model_id, "status": "missing", "reason": str(exc.__cause__ or exc)})
            continue
        problems = [] if args.skip_sha else verify_snapshot(spec, root)
        if args.skip_sha:
            from fnmatch import fnmatch

            present = [str(p.relative_to(root)) for p in root.rglob("*")]
            problems = [f"missing {pat}" for pat in spec.files if not any(fnmatch(n, pat) for n in present)]
        ok = ok and not problems
        report.append(
            {
                "model": model_id,
                "repo": spec.hf_repo,
                "revision": spec.revision,
                "path": str(root),
                "status": "ok" if not problems else "invalid",
                "problems": problems,
            }
        )
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
