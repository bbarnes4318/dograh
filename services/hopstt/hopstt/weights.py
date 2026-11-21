"""Resolve and fetch pinned model weights.

Weights live in ``HOPSTT_MODEL_CACHE`` using the Hugging Face hub cache
layout, always at the revision pinned in ``models.py`` (or overridden with
``HOPSTT_MODEL_REVISIONS``). At startup the server resolves them with
``local_files_only=True`` unless ``HOPSTT_ALLOW_DOWNLOAD=true``, so a
production worker never pulls an unpinned or unexpected file.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path

from .models import ModelSpec


class WeightsMissing(RuntimeError):
    pass


def snapshot_dir(spec: ModelSpec, settings, *, download: bool | None = None) -> Path:
    override = settings.model_paths.get(spec.id)
    if override:
        path = Path(override)
        if not path.exists():
            raise WeightsMissing(f"HOPSTT_MODEL_PATHS entry for {spec.id} does not exist")
        return path if path.is_dir() else path.parent
    if not spec.hf_repo:
        raise WeightsMissing(f"{spec.id} has no Hugging Face repo")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise WeightsMissing("huggingface_hub is not installed") from exc
    allow = download if download is not None else settings.allow_download
    try:
        return Path(
            snapshot_download(
                repo_id=spec.hf_repo,
                revision=spec.revision,
                cache_dir=settings.model_cache,
                allow_patterns=list(spec.files) or None,
                local_files_only=not allow,
            )
        )
    except Exception as exc:
        raise WeightsMissing(
            f"{spec.id}@{spec.revision} is not in {settings.model_cache}; "
            "run `python -m hopstt.tools.fetch_models` first"
        ) from exc


def weight_file(spec: ModelSpec, settings) -> Path:
    """Path of the primary weight file (the first entry of ``spec.files``)."""
    override = settings.model_paths.get(spec.id)
    if override and Path(override).is_file():
        return Path(override)
    root = snapshot_dir(spec, settings)
    path = root / spec.files[0]
    if not path.exists():
        raise WeightsMissing(f"{path} is missing")
    return path


def sha256_file(path: Path, block: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def verify_snapshot(spec: ModelSpec, root: Path) -> list[str]:
    """Return a list of problems (empty when every expected file exists and
    every published sha256 matches)."""
    problems: list[str] = []
    present = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()]
    for pattern in spec.files:
        if not any(fnmatch.fnmatch(name, pattern) for name in present):
            problems.append(f"missing {pattern}")
    for name, expected in spec.sha256.items():
        path = root / name
        if not path.exists():
            problems.append(f"missing {name}")
            continue
        actual = sha256_file(Path(os.path.realpath(path)))
        if actual != expected:
            problems.append(f"sha256 mismatch for {name}: {actual} != {expected}")
    return problems
