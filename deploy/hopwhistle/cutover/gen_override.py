#!/usr/bin/env python3
"""Turn the box's current docker-compose.override.yaml into the cutover one.

    python3 gen_override.py <current-override> <image-tag> <out-file> \
        [KEY=VALUE ...]

In the ``api:`` service block it:

* sets ``image:`` to the fork-built tag,
* removes every bind mount under ``volumes:`` (all of them are ported into
  the fork image; mounting the old files over it would break it),
* adds/overwrites the given environment variables.

Everything else (the ui service, FISH_API_KEY, DOGRAH_STATE_CID_POLICY, ...)
is copied through unchanged. Text-based on purpose: the box has no PyYAML,
and the file is small and hand-written. ``docker compose config`` validates
the result afterwards.
"""

from __future__ import annotations

import re
import sys


def transform(text: str, image: str, env: dict[str, str]) -> str:
    text = text.lstrip("﻿")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    in_api = False
    saw_api = False
    env_written = False

    def emit_env_block(indent: str) -> list[str]:
        return [f'{indent}  {k}: "{v}"' for k, v in env.items()]

    while i < len(lines):
        line = lines[i]
        service = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if service:
            if in_api and not env_written:
                out.append("    environment:")
                out.extend(emit_env_block("    "))
                env_written = True
            in_api = service.group(1) == "api"
            if in_api:
                saw_api = True
                out.append(line)
                out.append(f"    image: {image}")
                i += 1
                continue
        if in_api:
            if re.match(r"^    image:", line):
                i += 1
                continue
            if re.match(r"^    volumes:\s*$", line):
                i += 1
                while i < len(lines) and re.match(r"^      ", lines[i]):
                    i += 1
                continue
            if re.match(r"^    environment:\s*$", line):
                out.append(line)
                i += 1
                existing: list[str] = []
                while i < len(lines) and re.match(r"^      ", lines[i]):
                    key = lines[i].strip().split(":", 1)[0]
                    if key not in env:
                        existing.append(lines[i])
                    i += 1
                out.extend(existing)
                out.extend(emit_env_block("    "))
                env_written = True
                continue
        out.append(line)
        i += 1

    if in_api and not env_written:
        out.append("    environment:")
        out.extend(emit_env_block("    "))
    if not saw_api:
        raise SystemExit("no `api:` service in the override")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> None:
    if len(argv) < 4:
        raise SystemExit(__doc__)
    src, image, dst = argv[1], argv[2], argv[3]
    env: dict[str, str] = {}
    for pair in argv[4:]:
        key, _, value = pair.partition("=")
        env[key] = value
    with open(src, encoding="utf-8") as f:
        text = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(transform(text, image, env))


if __name__ == "__main__":
    main(sys.argv)
