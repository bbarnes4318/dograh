"""Read and rewrite the box's docker-compose.override.yaml without PyYAML.

The override is small and hand-written, so this is a deliberately narrow
line parser: two-space service indent, short-form volume entries and either
form of ``environment``. Anything it does not understand in a service block
is reported as ``unsupported`` instead of being guessed at, and the planner
treats that as a blocker. ``docker compose config`` still validates the
generated file before it is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SERVICE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$")
_KEY = re.compile(r"^    ([A-Za-z0-9_.-]+):\s*(.*?)\s*$")
_ITEM = re.compile(r"^      -\s*(.*?)\s*$")
_MAP_ITEM = re.compile(r"^      ([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")


class OverrideError(ValueError):
    pass


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _strip_comment(value: str) -> str:
    # Only a comment preceded by whitespace; '#' can appear inside values.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


@dataclass
class Volume:
    raw: str
    source: str
    target: str
    mode: str
    line: int

    @property
    def is_bind(self) -> bool:
        return self.source.startswith(("/", "./", "../", "~"))


@dataclass
class Service:
    name: str
    start: int
    end: int
    image: str | None = None
    image_line: int | None = None
    volumes: list[Volume] = field(default_factory=list)
    volumes_line: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_lines: dict[str, int] = field(default_factory=dict)
    env_style: str | None = None  # "map" | "list"
    env_line: int | None = None
    has_env_file: bool = False
    unsupported: list[str] = field(default_factory=list)


@dataclass
class Override:
    lines: list[str]
    services: dict[str, Service]


def parse(text: str) -> Override:
    text = text.lstrip("﻿")
    if "\t" in text:
        raise OverrideError("override contains tab characters")
    lines = text.splitlines()
    try:
        services_at = next(
            i for i, l in enumerate(lines) if re.match(r"^services:\s*$", l)
        )
    except StopIteration as exc:
        raise OverrideError("override has no top-level services: key") from exc
    section_end = next(
        (
            i
            for i in range(services_at + 1, len(lines))
            if re.match(r"^[^\s#]", lines[i])
        ),
        len(lines),
    )
    starts = [
        i for i in range(services_at + 1, section_end) if _SERVICE.match(lines[i])
    ]
    services: dict[str, Service] = {}
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else section_end
        svc = Service(_SERVICE.match(lines[start]).group(1), start, end)
        _parse_service(lines, svc)
        services[svc.name] = svc
    return Override(lines, services)


def _parse_service(lines: list[str], svc: Service) -> None:
    i = svc.start + 1
    while i < svc.end:
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        key = _KEY.match(line)
        if not key:
            svc.unsupported.append(f"line {i + 1}: unexpected indentation")
            i += 1
            continue
        name, value = key.group(1), _strip_comment(key.group(2))
        i += 1
        body_start = i
        while i < svc.end and (lines[i].startswith("      ") or not lines[i].strip()):
            i += 1
        body = range(body_start, i)
        if name == "image":
            svc.image, svc.image_line = _unquote(value), body_start - 1
        elif name == "volumes":
            svc.volumes_line = body_start - 1
            if value:
                svc.unsupported.append("inline volumes list")
            _parse_volumes(lines, body, svc)
        elif name == "environment":
            svc.env_line = body_start - 1
            if value:
                svc.unsupported.append("inline environment")
            _parse_env(lines, body, svc)
        elif name == "env_file":
            svc.has_env_file = True


def _parse_volumes(lines: list[str], body: range, svc: Service) -> None:
    for j in body:
        line = lines[j]
        if not line.strip() or line.strip().startswith("#"):
            continue
        item = _ITEM.match(line)
        if not item:
            svc.unsupported.append(f"line {j + 1}: long-form or nested volume entry")
            continue
        raw = _unquote(_strip_comment(item.group(1)))
        if ":" not in raw or raw.startswith(("type:", "{")):
            svc.unsupported.append(f"line {j + 1}: volume entry is not short form")
            continue
        parts = raw.split(":")
        if len(parts) == 2:
            source, target, mode = parts[0], parts[1], ""
        elif len(parts) == 3:
            source, target, mode = parts
        else:
            svc.unsupported.append(f"line {j + 1}: cannot split volume entry")
            continue
        svc.volumes.append(Volume(raw, source, target, mode, j))


def _parse_env(lines: list[str], body: range, svc: Service) -> None:
    for j in body:
        line = lines[j]
        if not line.strip() or line.strip().startswith("#"):
            continue
        item = _ITEM.match(line)
        if item:
            svc.env_style = svc.env_style or "list"
            if svc.env_style != "list":
                svc.unsupported.append("mixed environment styles")
            key, _, value = _unquote(item.group(1)).partition("=")
            svc.env[key.strip()] = value
            svc.env_lines[key.strip()] = j
            continue
        pair = _MAP_ITEM.match(line)
        if pair:
            svc.env_style = svc.env_style or "map"
            if svc.env_style != "map":
                svc.unsupported.append("mixed environment styles")
            svc.env[pair.group(1)] = _unquote(_strip_comment(pair.group(2)))
            svc.env_lines[pair.group(1)] = j
            continue
        svc.unsupported.append(f"line {j + 1}: unrecognised environment entry")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rewrite_service(
    override: Override,
    service: str,
    image: str,
    remove_targets: set[str],
    set_env: dict[str, str],
) -> str:
    """New override text: ``service`` gets ``image``, loses the bind mounts
    whose target is in ``remove_targets`` and gets ``set_env``. Every other
    line of the file is kept as is."""
    svc = override.services.get(service)
    if svc is None:
        raise OverrideError(f"no {service}: service in the override")
    if svc.unsupported:
        raise OverrideError(f"{service}: unsupported syntax: {svc.unsupported}")
    lines = list(override.lines)
    drop: set[int] = set()
    replace: dict[int, str] = {}
    insert_after: dict[int, list[str]] = {}

    kept_volumes = [v for v in svc.volumes if v.target not in remove_targets]
    for v in svc.volumes:
        if v.target in remove_targets:
            drop.add(v.line)
    if svc.volumes_line is not None and not kept_volumes:
        drop.add(svc.volumes_line)
        # Comments and blanks inside the emptied block go too.
        j = svc.volumes_line + 1
        while j < svc.end and (lines[j].startswith("      ") or not lines[j].strip()):
            drop.add(j)
            j += 1

    if svc.image_line is not None:
        replace[svc.image_line] = f"    image: {image}"
    else:
        insert_after.setdefault(svc.start, []).append(f"    image: {image}")

    pending = dict(set_env)
    for key, value in set_env.items():
        if key in svc.env_lines:
            replace[svc.env_lines[key]] = (
                f"      {key}: {_quote(value)}"
                if svc.env_style == "map"
                else f"      - {_quote(f'{key}={value}')}"
            )
            pending.pop(key)
    if pending:
        new = [
            f"      {k}: {_quote(v)}"
            if svc.env_style != "list"
            else f"      - {_quote(f'{k}={v}')}"
            for k, v in pending.items()
        ]
        if svc.env_line is not None:
            last = max([svc.env_line, *svc.env_lines.values()])
            insert_after.setdefault(last, []).extend(new)
        else:
            last = (
                max(
                    j
                    for j in range(svc.start, svc.end)
                    if j not in drop and lines[j].strip()
                )
                if any(lines[j].strip() for j in range(svc.start, svc.end))
                else svc.start
            )
            insert_after.setdefault(last, []).extend(["    environment:", *new])

    out: list[str] = []
    for j, line in enumerate(lines):
        if j not in drop:
            out.append(replace.get(j, line))
        out.extend(insert_after.get(j, []))
    return "\n".join(out) + "\n"
