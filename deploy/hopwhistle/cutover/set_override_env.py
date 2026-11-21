#!/usr/bin/env python3
"""Set environment variables on the api service in docker-compose.override.yaml.

Reads a JSON object of {NAME: value} on stdin (so secret values never appear
in argv or shell history), rewrites only those env lines with the same
line-preserving rewriter the cutover uses, and prints the names it set (never
the values). The image line and every other line stay as they are.

    printf '%s' '{"ASSEMBLYAI_STT_ROLLOUT": "canary"}' | python3 set_override_env.py FILE
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cutover_lib.compose import OverrideError, parse, rewrite_service  # noqa: E402

NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: set_override_env.py OVERRIDE_FILE < values.json", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    values = json.loads(sys.stdin.read() or "{}")
    if not isinstance(values, dict) or not values:
        print("no values given", file=sys.stderr)
        return 2
    for name, value in values.items():
        if not NAME.match(str(name)) or not isinstance(value, str) or "\n" in value:
            print(f"refusing invalid entry for {name!r}", file=sys.stderr)
            return 2
    try:
        override = parse(path.read_text())
        svc = override.services.get("api")
        if svc is None or not svc.image:
            raise OverrideError("no api service with an image line in the override")
        text = rewrite_service(override, "api", svc.image, set(), values)
    except OverrideError as exc:
        print(f"override not changed: {exc}", file=sys.stderr)
        return 1
    path.write_text(text)
    print("set: " + ", ".join(sorted(values)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
