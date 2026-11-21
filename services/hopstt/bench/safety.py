"""Data-safety helpers shared by every bench tool.

Rules enforced here (see DATASET.md "Access control and storage"):
- the private corpus and every output directory must live OUTSIDE any git
  working tree, unless ``--allow-in-repo`` is passed (synthetic fixtures only);
- output directories are created owner-only (0700) and private files 0600;
- default output location is outside the repo (``$HOPSTT_BENCH_OUT`` or
  ``~/hopstt-bench-results``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

CORPUS_ENV = "HOPSTT_EVAL_CORPUS"
OUT_ENV = "HOPSTT_BENCH_OUT"


class UnsafePathError(SystemExit):
    """A corpus/output path would put private data inside a git working tree.

    A SystemExit subclass so CLIs print the message and exit 1 without a traceback.
    """


def find_git_root(path: str | os.PathLike) -> Path | None:
    """Return the enclosing git working tree root of ``path`` (which may not exist yet)."""
    p = Path(path).expanduser().absolute()
    # Walk up to the nearest existing ancestor first, then look for .git upwards.
    while not p.exists() and p != p.parent:
        p = p.parent
    p = p.resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def guard_path(path: str | os.PathLike, *, what: str, allow_in_repo: bool = False) -> Path:
    """Refuse ``path`` if it is inside a git working tree (unless allowed)."""
    resolved = Path(path).expanduser().absolute()
    root = find_git_root(resolved)
    if root is not None and not allow_in_repo:
        raise UnsafePathError(
            f"refusing {what} path {resolved}: it is inside the git working tree {root}. "
            "Real recordings, transcripts and results contain PII and must live outside "
            "any repository. Pass --allow-in-repo only for synthetic fixtures."
        )
    return resolved


def resolve_corpus(corpus: str | None, *, allow_in_repo: bool = False) -> Path:
    value = corpus or os.environ.get(CORPUS_ENV)
    if not value:
        raise SystemExit(f"no corpus given: pass --corpus or set {CORPUS_ENV}")
    path = guard_path(value, what="corpus", allow_in_repo=allow_in_repo)
    if not path.is_dir():
        raise SystemExit(f"corpus directory {path} does not exist")
    return path


def default_output_dir(prefix: str) -> Path:
    base = os.environ.get(OUT_ENV) or str(Path.home() / "hopstt-bench-results")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(base) / f"{prefix}-{stamp}"


def prepare_output_dir(out: str | None, *, prefix: str, allow_in_repo: bool = False) -> Path:
    path = guard_path(out or default_output_dir(prefix), what="output", allow_in_repo=allow_in_repo)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def write_private(path: Path, text: str) -> None:
    """Write a file that may contain PII (transcripts, id maps) as owner-only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_jsonl_private(path: Path, rows) -> None:
    write_private(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def add_safety_args(parser) -> None:
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="allow corpus/output paths inside a git working tree (synthetic fixtures only)",
    )
