"""What to do when an image switch fails, and which manual choices are safe.

Rules:

* Image rollback and database recovery are separate decisions.
* The previous image is started automatically only when the database is
  provably still where that image left it: same single revision, no pending
  migration that steps outside a transaction (which can leave schema changes
  behind without moving the revision), no invalid indexes, and the new image
  never reported healthy (so it cannot have written rows the old code cannot
  read).
* The schema is never downgraded and a backup is never restored
  automatically. Those are operator commands with explicit preconditions.
* A container that is still starting is never stopped automatically: it may
  be in the middle of a long concurrent index build.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .migrations import RevisionGraph

# Phases recorded in the state file, in order.
PHASES = (
    "planned",
    "backed_up",
    "override_replaced",
    "recreating",
    "healthy",
    "verified",
)


@dataclass
class Facts:
    phase: str
    container: (
        str  # healthy | starting | unhealthy | exited | restarting | missing | unknown
    )
    db_revisions: list[str] | None  # None: could not be read
    pre_revision: str
    target_head: str
    old_graph: RevisionGraph
    new_graph: RevisionGraph
    new_image_was_healthy: bool = False
    invalid_indexes: list[str] = field(default_factory=list)
    rows_with_new_enum_values: int | None = 0  # None: could not be checked


@dataclass
class Decision:
    action: str
    automatic: bool
    summary: str
    reasons: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)


def forward_revisions(facts: Facts) -> list[str]:
    """Revisions applied beyond what the old image knows."""
    if not facts.db_revisions:
        return []
    applied: set[str] = set()
    for rev in facts.db_revisions:
        if rev in facts.new_graph.migrations:
            applied |= facts.new_graph.ancestors(rev)
    return sorted(applied - set(facts.old_graph.migrations))


def pending_migrations(facts: Facts) -> list[str]:
    if facts.pre_revision not in facts.new_graph.migrations:
        return []
    return facts.new_graph.upgrade_path(facts.pre_revision, facts.target_head)


def old_image_compatible(facts: Facts) -> tuple[bool, list[str]]:
    """Can the previous image start against the database as it is now,
    without touching the schema or alembic_version?"""
    reasons = []
    if facts.db_revisions is None:
        return False, ["database revision could not be read"]
    if len(facts.db_revisions) != 1:
        reasons.append(f"alembic_version has {len(facts.db_revisions)} rows")
    elif facts.db_revisions[0] != facts.pre_revision:
        reasons.append(
            f"database is on {facts.db_revisions[0]}, not the previous image's {facts.pre_revision}"
        )
    pending = pending_migrations(facts)
    if pending:
        first = facts.new_graph.migrations[pending[0]]
        if first.non_transactional:
            reasons.append(
                f"first pending migration {first.revision} runs outside a transaction, so it can "
                "change the schema without moving alembic_version"
            )
    if facts.invalid_indexes:
        reasons.append(f"invalid indexes present: {', '.join(facts.invalid_indexes)}")
    if facts.new_image_was_healthy:
        reasons.append(
            "the new image served traffic and may have written rows the old code cannot read"
        )
    return not reasons, reasons


def stamp_back_check(facts: Facts) -> tuple[bool, list[str]]:
    """Preconditions for the manual 'run the old image on the forward schema'
    choice (stamp alembic_version back to the old head, keep the schema)."""
    reasons = []
    if facts.db_revisions is None or len(facts.db_revisions) != 1:
        reasons.append("database revision is unreadable or not a single row")
        return False, reasons
    if facts.db_revisions[0] not in facts.new_graph.migrations:
        reasons.append(
            f"database revision {facts.db_revisions[0]} is unknown to both images"
        )
        return False, reasons
    for rev in forward_revisions(facts):
        m = facts.new_graph.migrations[rev]
        if m.risk == "review":
            reasons.append(
                f"{rev} is not purely additive ({'; '.join(m.risk_reasons)})"
            )
    if facts.invalid_indexes:
        reasons.append(f"invalid indexes present: {', '.join(facts.invalid_indexes)}")
    if facts.rows_with_new_enum_values is None:
        reasons.append(
            "could not check for rows using enum values the old code does not know"
        )
    elif facts.rows_with_new_enum_values:
        reasons.append(
            f"{facts.rows_with_new_enum_values} row(s) use enum values the old code cannot read"
        )
    return not reasons, reasons


def recovery_options(facts: Facts) -> list[str]:
    opts = ["status: read-only report of everything below (recover.sh status)"]
    db_known = facts.db_revisions is not None and all(
        r in facts.new_graph.migrations for r in facts.db_revisions
    )
    if db_known and not facts.invalid_indexes:
        opts.append(
            "resume-forward: start the new image again; it is compatible with every revision in "
            "its graph and re-runs the remaining (idempotent) migrations (recover.sh resume-forward)"
        )
    elif facts.invalid_indexes:
        opts.append(
            "drop the invalid indexes listed in status (DROP INDEX CONCURRENTLY ...), then resume-forward"
        )
    ok, _ = old_image_compatible(facts)
    if ok:
        opts.append(
            "image-rollback: start the previous image; the database is untouched (recover.sh image-rollback)"
        )
    else:
        ok_stamp, _ = stamp_back_check(facts)
        if ok_stamp:
            opts.append(
                "image-rollback --stamp-old-head: run the previous image on the forward schema; "
                "only alembic_version is changed, the additive schema stays (recover.sh image-rollback --stamp-old-head)"
            )
    opts.append("hold: keep api stopped while you investigate (recover.sh hold)")
    opts.append(
        "restore-db: restore the pre-cutover pg_dump, then image-rollback. Manual, destructive for "
        "anything written since the backup; see README.md 'Database recovery'"
    )
    return opts


def decide(facts: Facts) -> Decision:
    """The single automatic action a failing switch may take."""
    if facts.phase in ("planned", "backed_up"):
        return Decision(
            "nothing_changed",
            True,
            "failed before the override was replaced; nothing running changed",
        )
    if facts.phase == "override_replaced":
        return Decision(
            "restore_override",
            True,
            "failed after the override was replaced but before api was recreated; the running "
            "container is still the previous one, so only the override file is restored",
        )

    if facts.db_revisions is None:
        return Decision(
            "hold",
            False,
            "database revision could not be read; nothing is started or stopped automatically",
            ["database unreadable"],
            recovery_options(facts),
        )

    new_compatible = all(r in facts.new_graph.migrations for r in facts.db_revisions)
    if facts.container == "healthy" and new_compatible:
        return Decision(
            "keep_new_image",
            False,
            "the new image is healthy and compatible with the database; it stays in place while "
            "you resolve the failed check",
            [],
            recovery_options(facts),
        )
    if facts.container in ("starting", "unknown"):
        return Decision(
            "hold_running",
            False,
            "api is still starting (possibly building an index); it is left running. Check "
            "recover.sh status before deciding",
            [],
            recovery_options(facts),
        )

    ok, reasons = old_image_compatible(facts)
    if ok:
        return Decision(
            "auto_image_rollback",
            True,
            "the database is untouched; restoring the previous override and image",
        )
    return Decision(
        "hold_stopped",
        False,
        "the database may have moved past what the previous image understands; api is left "
        "stopped and nothing is rolled back automatically",
        reasons,
        recovery_options(facts),
    )
