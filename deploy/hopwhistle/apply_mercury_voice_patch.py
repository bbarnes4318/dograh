#!/usr/bin/env python3
"""Add Inception Mercury Voice to this box's Inception provider.

Why this exists
---------------
The fork's ``main`` adds Mercury Voice as a selectable Inception model and
fixes the temperature Dograh sends to Inception. While `hopwhistle-prod-ash`
still runs the stock image with files bind-mounted from `/opt/dograh-patches/`,
those patched files are what the API actually runs, so this script applies the
same change to them as anchored edits, in the style of
`apply_mercury3_patch.py`:

- ``registry.py``: ``mercury-voice`` is listed next to ``mercury-3`` (which
  stays the default), with helpers for which reasoning efforts each model
  accepts. Mercury Voice has no ``instant`` tier.
- ``check_validity.py``: saving Mercury Voice with ``instant`` is rejected with
  a clear message.
- ``service_factory.py``: at call time an unsupported effort is mapped to the
  nearest supported one (Voice + ``instant`` runs as ``low``), and Inception is
  called with temperature 0.5 instead of 0.1. Mercury 3 and Mercury Voice only
  accept 0.5 to 1.0.

Mercury Voice is enterprise-only: Inception has to enable it on the account
before calls with it succeed. Its model ID isn't published; ``mercury-voice``
is a placeholder, and any typed model ID containing "voice" gets the same
effort limits.

Run ``apply_inception_patch.py`` and ``apply_mercury3_patch.py`` first. This
script refuses to run until both are in place.

If the box has already been cut over to a fork-built image (see
``cutover/README.md``), this script is not needed: ``cutover/update_api.sh``
deploys ``main``, which already has the change. The script detects that and
exits without touching anything.

Usage
-----
    # 1. see what would change, touching nothing
    python3 apply_mercury_voice_patch.py --dry-run

    # 2. apply (writes .bak-mercury-voice-<timestamp> next to each file)
    python3 apply_mercury_voice_patch.py

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
    "registry.py": '"mercury-voice"',
    "check_validity.py": "does not support reasoning effort",
    "service_factory.py": "_inception_reasoning_effort",
}

# Earlier patches this one builds on, and the string that shows each is in.
PREREQUISITES = {
    "registry.py": ('"mercury-3"', "apply_mercury3_patch.py"),
    "check_validity.py": ("_check_inception_api_key", "apply_inception_patch.py"),
    "service_factory.py": (
        "_migrate_retired_inception_model",
        "apply_mercury3_patch.py",
    ),
}

# ---------------------------------------------------------------- registry.py

REGISTRY_EDITS = [
    (
        '        "reasoning effort knob to trade latency against depth."\n',
        '        "reasoning effort knob to trade latency against depth. Mercury "\n'
        '        "Voice is tuned for voice agents (enterprise access from Inception) "\n'
        '        "and accepts only low, medium or high reasoning effort."\n',
    ),
    (
        'INCEPTION_MODELS = [\n    "mercury-3",\n]\n',
        "# Mercury Voice: tuned for low-latency voice agents. Enterprise-only, and\n"
        "# the model ID is a placeholder until Inception confirms it.\n"
        'INCEPTION_VOICE_MODEL = "mercury-voice"\n'
        "INCEPTION_MODELS = [\n"
        '    "mercury-3",\n'
        "    INCEPTION_VOICE_MODEL,\n"
        "]\n",
    ),
    (
        'INCEPTION_DEFAULT_REASONING_EFFORT = "low"\n',
        'INCEPTION_DEFAULT_REASONING_EFFORT = "low"\n'
        'INCEPTION_REASONING_EFFORTS = ("instant", "low", "medium", "high")\n'
        '# Mercury Voice has no "instant" tier.\n'
        'INCEPTION_VOICE_REASONING_EFFORTS = ("low", "medium", "high")\n'
        "# Mercury 3 and Mercury Voice only accept temperatures from 0.5 to 1.0.\n"
        "INCEPTION_MIN_TEMPERATURE = 0.5\n"
        "\n"
        "\n"
        "def is_inception_voice_model(model: str | None) -> bool:\n"
        '    """True for Mercury Voice, whatever exact ID Inception gives it."""\n'
        "    return bool(model) and (\n"
        '        model == INCEPTION_VOICE_MODEL or "voice" in model.lower()\n'
        "    )\n"
        "\n"
        "\n"
        "def inception_reasoning_efforts(model: str | None) -> tuple[str, ...]:\n"
        '    """The reasoning_effort values the given Inception model accepts."""\n'
        "    if is_inception_voice_model(model):\n"
        "        return INCEPTION_VOICE_REASONING_EFFORTS\n"
        "    return INCEPTION_REASONING_EFFORTS\n",
    ),
]

# ----------------------------------------------------------- check_validity.py

# Self-contained (no new import) so the edit doesn't depend on how the patched
# file's registry import block is laid out.
CHECK_VALIDITY_EDITS = [
    (
        "        a transient upstream problem can't lock a user out of their own config.\n"
        '        """\n'
        "        base_url = (\n",
        "        a transient upstream problem can't lock a user out of their own config.\n"
        "\n"
        "        Also rejects a reasoning effort the chosen model doesn't accept\n"
        '        (Mercury Voice has no "instant" tier).\n'
        '        """\n'
        '        model_name = getattr(service_config, "model", None) or ""\n'
        '        effort = getattr(service_config, "reasoning_effort", None)\n'
        '        if "voice" in model_name.lower() and effort == "instant":\n'
        "            raise ValueError(\n"
        "                f\"Inception model '{model_name}' does not support reasoning effort \"\n"
        "                \"'instant'. Choose one of: low, medium, high.\"\n"
        "            )\n"
        "        base_url = (\n",
    ),
]

# ------------------------------------------------------------ service_factory.py

SERVICE_FACTORY_EDITS = [
    (
        "\n\ndef create_llm_service_from_provider(\n",
        "\n\n"
        "INCEPTION_MIN_TEMPERATURE = 0.5\n"
        "\n"
        "\n"
        "def _inception_reasoning_effort(model: str, effort: str | None) -> str:\n"
        '    """Mercury Voice has no "instant" tier; run it as "low" instead of failing."""\n'
        '    effort = effort or "low"\n'
        '    if "voice" in (model or "").lower() and effort == "instant":\n'
        "        logger.warning(\n"
        "            f\"Inception model '{model}' does not accept reasoning_effort \"\n"
        "            \"'instant'; using 'low' instead\"\n"
        "        )\n"
        '        return "low"\n'
        "    return effort\n"
        "\n"
        "\n"
        "def create_llm_service_from_provider(\n",
    ),
    (
        "                temperature=0.1,\n"
        "                reasoning_effort=reasoning_effort or INCEPTION_DEFAULT_REASONING_EFFORT,\n",
        "                # Mercury 3 and Mercury Voice reject temperatures below 0.5.\n"
        "                temperature=INCEPTION_MIN_TEMPERATURE,\n"
        "                reasoning_effort=_inception_reasoning_effort(model, reasoning_effort),\n",
    ),
]

EDITS = {
    "registry.py": REGISTRY_EDITS,
    "check_validity.py": CHECK_VALIDITY_EDITS,
    "service_factory.py": SERVICE_FACTORY_EDITS,
}


def box_uses_patch_mounts(compose_dir: Path) -> bool:
    """True when the api container still runs files from /opt/dograh-patches/."""
    override = compose_dir / "docker-compose.override.yaml"
    return override.exists() and "/opt/dograh-patches/" in override.read_text()


def plan_edits(patch_dir: Path) -> tuple[dict[str, tuple[Path, str]], list[str]]:
    """Read every file and verify every anchor. Writes nothing.

    Returns (plans, problems): plans maps file name to (path, new text) for
    files that need patching; problems lists everything that blocks applying.
    """
    plans: dict[str, tuple[Path, str]] = {}
    problems: list[str] = []

    for name, edits in EDITS.items():
        path = patch_dir / name
        if not path.exists():
            problems.append(f"{name}: missing at {path}")
            continue

        text = path.read_text()
        if IDEMPOTENCY_MARKERS[name] in text:
            print(f"  {name}: already patched, skipping")
            continue

        marker, prerequisite = PREREQUISITES[name]
        if marker not in text:
            problems.append(f"{name}: run {prerequisite} first")
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

    return plans, problems


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    if not box_uses_patch_mounts(args.compose_dir):
        print(
            "This box does not mount files from /opt/dograh-patches/, so it runs a\n"
            "fork-built image. Nothing to patch: deploy main with\n"
            "  sudo bash cutover/update_api.sh\n"
            "Nothing changed."
        )
        return 0

    plans, problems = plan_edits(args.patch_dir)

    if problems:
        print("\nNOT APPLYING ANYTHING. Problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if not plans:
        print("\nNothing to do — every file is already patched.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name, (path, text) in sorted(plans.items()):
        if args.dry_run:
            print(f"  {name}: would apply {len(EDITS[name])} edit(s)")
            continue
        backup = path.with_name(f"{path.name}.bak-mercury-voice-{stamp}")
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
        "  2. curl -s localhost:8000/api/v1/user/configurations/defaults"
        " | grep -o mercury-voice"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
