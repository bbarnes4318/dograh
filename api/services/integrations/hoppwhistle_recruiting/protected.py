"""Protected recruiting steps: a digest taken when Hoppwhistle publishes, and
the publish-gate check that refuses an editor change to them.

Hoppwhistle compiles each recruiting workflow from an interview framework.
The AI disclosure greeting, the locked rule blocks (``[[protected:<key>]]``
... ``[[/protected:<key>]]``), the scored questions inside them, the edges,
the tools and the extraction fields are protected. This digest covers exactly
those; agency guidance outside the blocks, positions and model settings are
free to change. A mismatch means someone edited a protected part in the
voice builder: publishing is refused here, and Hoppwhistle independently
refuses to dial a non-conforming published workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

RECRUITING_NODE_TYPE = "hoppwhistle_recruiting"
_BLOCK = re.compile(
    r"\[\[protected:([a-z0-9_]+)\]\]\n?([\s\S]*?)\n?\[\[/protected:\1\]\]"
)


def protected_blocks(text: str | None) -> list[str]:
    if not text:
        return []
    return [f"{m.group(1)}:{m.group(2).strip()}" for m in _BLOCK.finditer(text)]


def protected_digest(definition: dict[str, Any]) -> str:
    nodes = []
    for node in definition.get("nodes") or []:
        if node.get("type") == RECRUITING_NODE_TYPE:
            continue
        data = node.get("data") or {}
        nodes.append(
            {
                "id": node.get("id"),
                "type": node.get("type"),
                "blocks": protected_blocks(data.get("prompt")),
                "greeting": data.get("greeting")
                if node.get("type") == "startCall"
                else None,
                "tools": sorted(str(t) for t in (data.get("tool_uuids") or [])),
                "extraction": sorted(
                    str(v.get("name"))
                    for v in (data.get("extraction_variables") or [])
                    if isinstance(v, dict)
                ),
            }
        )
    edges = sorted(
        (
            str(e.get("source")),
            str(e.get("target")),
            str((e.get("data") or {}).get("condition")),
        )
        for e in definition.get("edges") or []
    )
    canonical = json.dumps(
        {"nodes": sorted(nodes, key=lambda n: str(n["id"])), "edges": edges},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_recruiting_definition(
    definition: dict[str, Any],
) -> list[tuple[str | None, str]]:
    """Publish gate: a recruiting workflow's protected parts must match its digest."""
    recruiting = [
        n
        for n in (definition.get("nodes") or [])
        if n.get("type") == RECRUITING_NODE_TYPE
    ]
    if not recruiting:
        return []
    node = recruiting[0]
    expected = (node.get("data") or {}).get("recruiting_protected_digest")
    if not expected:
        return [
            (
                node.get("id"),
                "This recruiting interview was not published from the Hoppwhistle AI Recruiting Studio. Publish it from the Studio.",
            )
        ]
    if protected_digest(definition) != expected:
        return [
            (
                node.get("id"),
                "Protected interview steps (AI disclosure, consent rules, scored questions, transitions, tools or "
                "extracted fields) were changed here. Make interview changes in the Hoppwhistle AI Recruiting "
                "Studio and republish from there; conversation guidance outside the locked blocks can be edited.",
            )
        ]
    return []
