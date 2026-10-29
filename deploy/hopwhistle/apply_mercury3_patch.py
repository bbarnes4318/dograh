#!/usr/bin/env python3
"""Move this box's Inception provider from Mercury 2.5 to Mercury 3.

Why this exists
---------------
Inception retires Mercury 2 and 2.5 on 2026-11-15; Mercury 3 is the same API
and key with a new model name. The fork's ``main`` made that change in
``registry.py`` and ``service_factory.py``, but while `hopwhistle-prod-ash`
still runs the stock image with files bind-mounted from `/opt/dograh-patches/`,
those two patched files are what the API actually runs. This script applies
the same change to them as anchored edits, in the style of
`apply_inception_patch.py`:

- ``registry.py``: ``mercury-3`` becomes the only listed model and the default.
- ``service_factory.py``: saved configs that still name ``mercury-2`` or
  ``mercury-2.5`` are upgraded to ``mercury-3`` when the LLM service is built,
  so existing agents keep working without being re-saved.

If the box has already been cut over to a fork-built image (see
``cutover/README.md``), this script is not needed: ``cutover/update_api.sh``
deploys ``main``, which already has the change. The script detects that and
exits without touching anything.

Usage
-----
    # 1. see what would change, touching nothing
    python3 apply_mercury3_patch.py --dry-run

    # 2. apply (writes .bak-mercury3-<timestamp> next to each file)
    python3 apply_mercury3_patch.py

    # 3. restart the api container to load the patched files
    cd /opt/dograh && docker compose up -d --force-recreate --no-deps api

Nothing is written unless every anchor for every file is found.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

PATCH_DIR = Path("/opt/dograh-patches")
COMPOSE_DIR = Path("/opt/dograh")

# A file is considered already patched if this string is present.
IDEMPOTENCY_MARKERS = {
    "registry.py": '"mercury-3"',
    "service_factory.py": "_migrate_retired_inception_model",
}

# ---------------------------------------------------------------- registry.py

REGISTRY_EDITS = [
    (
        '        "Inception Mercury diffusion LLMs. Mercury 2.5 decodes tokens in "\n',
        '        "Inception Mercury diffusion LLMs. Mercury 3 decodes tokens in "\n',
    ),
    (
        "INCEPTION_MODELS = [\n"
        '    "mercury-2.5",\n'
        '    "mercury-2",\n'
        "]\n"
        'INCEPTION_DEFAULT_MODEL = "mercury-2.5"\n',
        "INCEPTION_MODELS = [\n"
        '    "mercury-3",\n'
        "]\n"
        'INCEPTION_DEFAULT_MODEL = "mercury-3"\n'
        "# Inception retires these on 2026-11-15; stored configs that still name them\n"
        "# are upgraded to Mercury 3 at service creation.\n"
        'INCEPTION_RETIRED_MODELS = {"mercury-2", "mercury-2.5"}\n',
    ),
]

# ------------------------------------------------------------ service_factory.py

# The helper carries its own constants so the edit doesn't depend on how the
# patched file's registry import block is laid out.
SERVICE_FACTORY_EDITS = [
    (
        "\n\ndef create_llm_service_from_provider(\n",
        "\n\n"
        'INCEPTION_CURRENT_MODEL = "mercury-3"\n'
        'INCEPTION_RETIRED_MODELS = {"mercury-2", "mercury-2.5"}\n'
        "\n"
        "\n"
        "def _migrate_retired_inception_model(model: str) -> str:\n"
        '    """Inception retired Mercury 2 and 2.5 in favour of Mercury 3 (same API).\n'
        '    Upgrade stored configs that still reference them so calls keep working."""\n'
        "    if model in INCEPTION_RETIRED_MODELS:\n"
        "        logger.warning(\n"
        "            f\"Inception model '{model}' is retired; using '{INCEPTION_CURRENT_MODEL}' instead\"\n"
        "        )\n"
        "        return INCEPTION_CURRENT_MODEL\n"
        "    return model\n"
        "\n"
        "\n"
        "def create_llm_service_from_provider(\n",
    ),
    (
        "    elif provider == ServiceProviders.INCEPTION.value:\n"
        "        base_url = base_url or INCEPTION_DEFAULT_BASE_URL\n",
        "    elif provider == ServiceProviders.INCEPTION.value:\n"
        "        model = _migrate_retired_inception_model(model)\n"
        "        base_url = base_url or INCEPTION_DEFAULT_BASE_URL\n",
    ),
]

EDITS = {
    "registry.py": REGISTRY_EDITS,
    "service_factory.py": SERVICE_FACTORY_EDITS,
}


def box_uses_patch_mounts(compose_dir: Path) -> bool:
    """True when the api container still runs files from /opt/dograh-patches/."""
    override = compose_dir / "docker-compose.override.yaml"
    return override.exists() and "/opt/dograh-patches/" in override.read_text()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patch-dir", type=Path, default=PATCH_DIR, help="default /opt/dograh-patches"
    )
    parser.add_argument(
        "--compose-dir", type=Path, default=COMPOSE_DIR, help="default /opt/dograh"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    if not box_uses_patch_mounts(args.compose_dir):
        print(
            "This box does not mount files from /opt/dograh-patches/, so it runs a\n"
            "fork-built image. Nothing to patch: deploy main with\n"
            "  sudo bash cutover/update_api.sh\n"
            "Nothing changed."
        )
        return 0

    # ---- pass 1: read everything and verify every anchor before writing ----
    plans: dict[str, tuple[Path, str]] = {}
    problems: list[str] = []

    for name, edits in EDITS.items():
        path = args.patch_dir / name
        if not path.exists():
            problems.append(f"{name}: missing at {path}")
            continue

        text = path.read_text()
        if IDEMPOTENCY_MARKERS[name] in text:
            print(f"  {name}: already patched, skipping")
            continue

        for i, (find, replace) in enumerate(edits, start=1):
            count = text.count(find)
            if count != 1:
                problems.append(
                    f"{name}: anchor for edit {i} of {len(edits)} found {count} "
                    f"times, expected once — send me this file and I'll re-anchor it"
                )
                break
            text = text.replace(find, replace, 1)
        else:
            plans[name] = (path, text)

    if problems:
        print("\nNOT APPLYING ANYTHING. Problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if not plans:
        print("\nNothing to do — every file is already patched.")
        return 0

    # ---- pass 2: write ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name, (path, text) in sorted(plans.items()):
        if args.dry_run:
            print(f"  {name}: would apply {len(EDITS[name])} edit(s)")
            continue
        backup = path.with_name(f"{path.name}.bak-mercury3-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(text)
        print(f"  {name}: applied {len(EDITS[name])} edit(s)  (backup: {backup.name})")

    if args.dry_run:
        print("\nDry run — nothing written.")
        return 0

    print("\nPatched. Remaining steps:")
    print(
        f"  1. cd {args.compose_dir} && docker compose up -d --force-recreate --no-deps api"
    )
    print(
        "  2. curl -s localhost:8000/api/v1/user/configurations/defaults | grep -o mercury-3"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
