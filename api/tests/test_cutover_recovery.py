"""What a failed switch may do on its own, and which manual choices are
allowed, for each partial-failure state."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "deploy" / "hopwhistle" / "cutover"))

from cutover_lib import recovery
from cutover_lib.migrations import load_graph

ENUM_UP = (
    'op.sync_enum_values(enum_schema="public", enum_name="cat", new_values=["a", "b"], '
    'affected_columns=[TableReference(table_schema="public", table_name="tools", column_name="category")], '
    "enum_values_to_rename=[])"
)
ENUM_DOWN = ENUM_UP.replace('["a", "b"]', '["a"]')


def mig(rev, down, up="pass", down_body="pass"):
    source = (
        f'revision = "{rev}"\ndown_revision = {down!r}\nfrom alembic import op\n'
        f"def upgrade():\n    {up}\ndef downgrade():\n    {down_body}\n"
    )
    return (f"{rev}_m.py", source)


TRANSACTIONAL = 'op.add_column("t", sa.Column("c", sa.Text(), nullable=True))'
CONCURRENT = 'with op.get_context().autocommit_block():\n        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c)")'

OLD = load_graph([mig("base", None)])


def graph(first_up=TRANSACTIONAL):
    return load_graph(
        [
            mig("base", None),
            mig("m1", "base", first_up),
            mig("m2", "m1", ENUM_UP, ENUM_DOWN),
            mig("m3", "m2", 'op.drop_column("t", "c")'),
        ]
    )


def facts(**kw):
    base = {
        "phase": "recreating",
        "container": "exited",
        "db_revisions": ["base"],
        "pre_revision": "base",
        "target_head": "m3",
        "old_graph": OLD,
        "new_graph": graph(),
    }
    base.update(kw)
    return recovery.Facts(**base)


@pytest.mark.parametrize(
    "phase,action",
    [
        ("planned", "nothing_changed"),
        ("backed_up", "nothing_changed"),
        ("override_replaced", "restore_override"),
    ],
)
def test_failures_before_recreate_are_automatic_and_harmless(phase, action):
    d = recovery.decide(facts(phase=phase))
    assert d.action == action and d.automatic


def test_untouched_database_allows_automatic_image_rollback():
    d = recovery.decide(facts())
    assert d.action == "auto_image_rollback" and d.automatic


def test_non_transactional_first_migration_prevents_automatic_rollback():
    # The revision did not move, but CREATE INDEX CONCURRENTLY may have left
    # schema behind: unverified, so hold.
    d = recovery.decide(facts(new_graph=graph(CONCURRENT)))
    assert d.action == "hold_stopped" and not d.automatic
    assert any("outside a transaction" in r for r in d.reasons)


def test_partial_migration_holds_and_offers_choices():
    d = recovery.decide(facts(db_revisions=["m1"]))
    assert d.action == "hold_stopped" and not d.automatic
    assert any(o.startswith("resume-forward") for o in d.options)
    assert any(o.startswith("image-rollback --stamp-old-head") for o in d.options)
    assert any(o.startswith("restore-db") for o in d.options)


def test_new_image_that_served_traffic_is_never_rolled_back_automatically():
    d = recovery.decide(facts(new_image_was_healthy=True))
    assert d.action == "hold_stopped"
    assert any("served traffic" in r for r in d.reasons)


def test_healthy_compatible_new_image_stays_in_place():
    d = recovery.decide(facts(container="healthy", db_revisions=["m3"]))
    assert d.action == "keep_new_image" and not d.automatic


def test_starting_container_is_left_running():
    d = recovery.decide(facts(container="starting", db_revisions=["m1"]))
    assert d.action == "hold_running" and not d.automatic


def test_unreadable_database_holds():
    d = recovery.decide(facts(db_revisions=None))
    assert d.action == "hold" and not d.automatic


def test_invalid_index_blocks_rollback_and_resume():
    f = facts(invalid_indexes=["i"])
    assert recovery.decide(f).action == "hold_stopped"
    opts = recovery.recovery_options(f)
    assert not any(o.startswith("resume-forward") for o in opts)
    assert any("drop the invalid indexes" in o for o in opts)


class TestStampBack:
    def test_allowed_for_additive_and_enum_extension_without_new_rows(self):
        ok, reasons = recovery.stamp_back_check(facts(db_revisions=["m2"]))
        assert ok, reasons
        assert recovery.forward_revisions(facts(db_revisions=["m2"])) == ["m1", "m2"]

    def test_refused_when_rows_use_new_enum_values(self):
        ok, reasons = recovery.stamp_back_check(
            facts(db_revisions=["m2"], rows_with_new_enum_values=3)
        )
        assert not ok and any("enum values" in r for r in reasons)

    def test_refused_when_enum_rows_cannot_be_checked(self):
        ok, _ = recovery.stamp_back_check(
            facts(db_revisions=["m2"], rows_with_new_enum_values=None)
        )
        assert not ok

    def test_refused_after_a_destructive_migration(self):
        ok, reasons = recovery.stamp_back_check(facts(db_revisions=["m3"]))
        assert not ok and any("m3 is not purely additive" in r for r in reasons)

    def test_refused_for_revision_unknown_to_both_images(self):
        ok, _ = recovery.stamp_back_check(facts(db_revisions=["zzz"]))
        assert not ok


def test_old_image_compatibility_requires_exact_previous_revision():
    ok, _ = recovery.old_image_compatible(facts())
    assert ok
    ok, reasons = recovery.old_image_compatible(facts(db_revisions=["m1"]))
    assert not ok and any("not the previous image's" in r for r in reasons)
    ok, _ = recovery.old_image_compatible(facts(db_revisions=["base", "m1"]))
    assert not ok
