"""Assemble a redacted cutover/update plan from collected facts.

``build_plan`` is pure: every input is passed in, so each blocking rule is
covered by offline tests. The CLI (cutover_plan.py) collects the inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import compose
from .migrations import GraphError, RevisionGraph
from .mounts import MountEvaluation, shown

DEFAULT_MAX_PENDING = 20
_LONG_NUMBER = re.compile(r"(?<![0-9A-Za-z])\+?\d{10,}(?![0-9A-Za-z])")


def redact(text: str) -> str:
    """Last line of defence: no phone-number-like runs in anything printed."""
    return _LONG_NUMBER.sub("<number>", text)


@dataclass
class PlanInputs:
    mode: str  # cutover | update
    service: str
    target_image: str
    target_image_id: str
    target_graph: RevisionGraph | None
    target_graph_error: str | None
    running_image_id: str
    running_graph: RevisionGraph | None
    running_graph_error: str | None
    db_revisions: list[str] | None  # None: alembic_version missing
    override_text: str
    mounts: MountEvaluation | None
    max_pending: int = DEFAULT_MAX_PENDING
    allow_migrations: set[str] = field(default_factory=set)


@dataclass
class Plan:
    data: dict
    blockers: list[str]
    override_text: str | None

    @property
    def ok(self) -> bool:
        return not self.blockers


def _pending_entry(graph: RevisionGraph, rev: str) -> dict:
    m = graph.migrations[rev]
    return {
        "revision": rev,
        "file": m.filename,
        "risk": m.risk,
        "non_transactional": m.non_transactional,
        "reasons": m.risk_reasons,
        "enum_additions": [
            {
                "enum": e.enum_name,
                "values": e.added_values,
                "columns": [f"{t}.{c}" for t, c in e.columns],
            }
            for e in m.enum_extensions
        ],
    }


def build_plan(inp: PlanInputs) -> Plan:
    blockers: list[str] = []
    warnings: list[str] = []
    data: dict = {
        "schema": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": inp.mode,
        "service": inp.service,
        "target": {"image": inp.target_image, "image_id": inp.target_image_id},
        "running": {"image_id": inp.running_image_id},
        "database": {"revisions": inp.db_revisions},
    }

    # ---- revision graphs -------------------------------------------------
    tg, rg = inp.target_graph, inp.running_graph
    if tg is None:
        blockers.append(
            f"target image migrations are not a valid graph: {inp.target_graph_error}"
        )
    else:
        data["target"]["head"] = tg.heads[0]
        data["target"]["revision_count"] = len(tg.migrations)
    if rg is None:
        blockers.append(
            f"running container migrations are not a valid graph: {inp.running_graph_error}"
        )
    else:
        data["running"]["head"] = rg.heads[0]

    # ---- database state --------------------------------------------------
    db = inp.db_revisions
    pre: str | None = None
    if db is None:
        blockers.append(
            "alembic_version table is missing; this is not a migrated dograh database"
        )
    elif not db:
        blockers.append("alembic_version is empty; refusing to guess the schema state")
    elif len(db) > 1:
        blockers.append(
            f"alembic_version has {len(db)} rows ({', '.join(db)}); divergent heads are not supported"
        )
    else:
        pre = db[0]

    pending: list[str] = []
    if pre and tg and rg:
        if pre not in rg.migrations:
            blockers.append(f"database revision {pre} is unknown to the running image")
        elif pre != rg.heads[0]:
            blockers.append(
                f"database is on {pre} but the running image's head is {rg.heads[0]}; the running "
                "system is not in the state its own code expects"
            )
        if pre not in tg.migrations:
            blockers.append(
                f"database revision {pre} is unknown to the target image (database ahead of, or "
                "diverged from, the target)"
            )
        foreign = sorted(set(rg.migrations) - set(tg.migrations))
        if foreign:
            blockers.append(
                f"the running image has {len(foreign)} migration(s) the target lacks "
                f"(e.g. {', '.join(foreign[:3])}); the target is not a descendant"
            )
        if pre in tg.migrations and not foreign:
            try:
                pending = tg.upgrade_path(pre, tg.heads[0])
            except GraphError as exc:
                blockers.append(str(exc))
    data["migrations"] = {
        "from": pre,
        "to": tg.heads[0] if tg else None,
        "pending": [_pending_entry(tg, r) for r in pending] if tg else [],
    }
    if len(pending) > inp.max_pending:
        blockers.append(
            f"{len(pending)} pending migrations exceeds the supported maximum of {inp.max_pending}; "
            "upgrade in smaller steps or raise --max-pending after review"
        )
    for rev in pending:
        m = tg.migrations[rev]
        if m.risk == "review" and rev not in inp.allow_migrations:
            blockers.append(
                f"pending migration {rev} ({m.filename}) is not purely additive "
                f"({'; '.join(m.risk_reasons)}); review it and pass --allow-migration {rev}"
            )
    forward_risky = [r for r in pending if tg.migrations[r].risk == "review"]
    data["recovery"] = {
        "image_rollback_without_schema_change": not pending,
        "stamp_back_possible_after_migration": bool(pending) and not forward_risky,
        "non_transactional_pending": [
            r for r in pending if tg.migrations[r].non_transactional
        ],
        "enum_extensions_pending": [
            r for r in pending if tg.migrations[r].enum_extensions
        ],
    }
    if pending:
        warnings.append(
            "after these migrations the previous image cannot start without changing alembic_version; "
            "a failed switch after migrating holds instead of rolling back (see README.md)"
        )

    # ---- override and mounts --------------------------------------------
    override_text: str | None = None
    try:
        parsed = compose.parse(inp.override_text)
    except compose.OverrideError as exc:
        parsed = None
        blockers.append(f"override: {exc}")
    if parsed is not None:
        svc = parsed.services.get(inp.service)
        if svc is None:
            blockers.append(f"override has no {inp.service}: service")
        else:
            if svc.unsupported:
                blockers.append(
                    f"override {inp.service}: unsupported syntax ({'; '.join(svc.unsupported)})"
                )
            data["override"] = {
                "image": svc.image,
                "env_file": svc.has_env_file,
                "env_style": svc.env_style,
            }
        for name, other in parsed.services.items():
            if name == inp.service:
                continue
            shadow = [
                v.target for v in other.volumes if v.target.startswith("/app/api")
            ]
            if shadow:
                blockers.append(
                    f"service {name} also mounts over /app/api ({', '.join(shadow)}); it would keep "
                    "running patched code. Handle that service first"
                )

    ev = inp.mounts
    if ev is not None:
        data["mounts"] = [
            {
                "target": r.target,
                "action": r.action,
                "sha256": r.sha256,
                "target_sha256": r.target_sha256,
                "variants": r.variants,
                "behaviors": r.behaviors,
                "reasons": r.reasons,
            }
            for r in ev.results
        ]
        blockers.extend(ev.blockers)
        warnings.extend(ev.warnings)
        data["environment"] = {
            name: {"value": shown(name, value), "origin": ev.env_origin.get(name, "")}
            for name, value in sorted(ev.env.items())
        }
        if ev.env.get(
            "CAMPAIGN_TRANSFER_DESTINATION_POLICY"
        ) == "forced_default" and not ev.env.get(
            "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"
        ):
            blockers.append(
                "forced_default transfer policy without CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"
            )

    if not blockers and parsed is not None and ev is not None:
        try:
            override_text = compose.rewrite_service(
                parsed,
                inp.service,
                inp.target_image,
                {r.target for r in ev.results if r.action == "remove"},
                ev.env,
            )
        except compose.OverrideError as exc:
            blockers.append(f"override: {exc}")

    data["blockers"] = [redact(b) for b in blockers]
    data["warnings"] = [redact(w) for w in warnings]
    data["fingerprint"] = fingerprint(
        inp.override_text, inp.db_revisions, inp.target_image_id, inp.running_image_id
    )
    return Plan(data, data["blockers"], override_text)


def fingerprint(
    override_text: str, db: list[str] | None, target_id: str, running_id: str
) -> str:
    """Identifies the state a plan was made against; the switch refuses to run
    if any of it changed in between."""
    h = hashlib.sha256()
    for part in (override_text, json.dumps(db), target_id, running_id):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def render(data: dict) -> str:
    lines = [
        f"PLAN ({data['mode']}) for service {data['service']}",
        f"  target image : {data['target'].get('image')} ({data['target'].get('image_id', '')[:19]})",
        f"  target head  : {data['target'].get('head')}",
        f"  running head : {data['running'].get('head')}",
        f"  database     : {data['database'].get('revisions')}",
    ]
    mig = data.get("migrations", {})
    if mig.get("pending"):
        lines.append(
            f"  migrations   : {mig['from']} -> {mig['to']} ({len(mig['pending'])} pending)"
        )
        for p in mig["pending"]:
            flags = [p["risk"]] + (
                ["non-transactional"] if p["non_transactional"] else []
            )
            lines.append(f"    - {p['revision']} {p['file']} [{', '.join(flags)}]")
    else:
        lines.append("  migrations   : none")
    for m in data.get("mounts", []):
        lines.append(f"  mount {m['action']:<6} {m['target']}")
        for b in m["behaviors"]:
            lines.append(f"      carries {b}")
        for r in m["reasons"]:
            lines.append(f"      {r}")
    if data.get("environment"):
        lines.append("  environment to set:")
        for name, info in data["environment"].items():
            lines.append(f"    {name}={info['value']}  ({info['origin']})")
    rec = data.get("recovery", {})
    lines.append(
        "  recovery     : image rollback without schema change: "
        f"{'yes' if rec.get('image_rollback_without_schema_change') else 'NO (migrations pending)'}"
    )
    for w in data.get("warnings", []):
        lines.append(f"  WARNING: {w}")
    for b in data.get("blockers", []):
        lines.append(f"  BLOCKED: {b}")
    lines.append("  result       : " + ("BLOCKED" if data.get("blockers") else "OK"))
    return redact("\n".join(lines)) + "\n"
