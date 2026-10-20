"""Static alembic graph reading for the hopwhistle cutover planner."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "deploy" / "hopwhistle" / "cutover"))

from cutover_lib.migrations import GraphError, load_graph, load_graph_from_dir

VERSIONS = REPO / "api" / "alembic" / "versions"


def mig(rev, down, body="pass", extra=""):
    source = (
        f'revision = "{rev}"\ndown_revision = {down!r}\n{extra}\n'
        f"from alembic import op\nimport sqlalchemy as sa\n\n"
        f"def upgrade():\n    {body}\n\ndef downgrade():\n    pass\n"
    )
    return (f"{rev}_m.py", source)


def test_repository_graph_is_valid_and_single_headed():
    graph = load_graph_from_dir(str(VERSIONS))
    assert len(graph.heads) == 1
    assert len(graph.roots) == 1
    # Every file with a revision is in the graph.
    assert len(graph.migrations) == len(
        [p for p in VERSIONS.glob("*.py") if not p.name.startswith("__")]
    )


def test_upgrade_path_from_old_cutover_base_is_derived_not_hardcoded():
    graph = load_graph_from_dir(str(VERSIONS))
    path = graph.upgrade_path("00b0201ad918", graph.heads[0])
    # The old scripts assumed exactly one migration (b7c41d9e52aa). The graph
    # says otherwise; this test fails loudly if the chain changes again.
    assert path[0] == "b7c41d9e52aa"
    assert path[-1] == graph.heads[0]
    assert len(path) >= 4
    risks = {r: graph.migrations[r].risk for r in path[:4]}
    assert risks == {
        "b7c41d9e52aa": "additive",
        "9d3e1a7c5b20": "enum_extension",
        "a4c8e2f61b93": "enum_extension",
        "c3f9a1d7e4b2": "additive",
    }
    assert graph.migrations["b7c41d9e52aa"].non_transactional
    ext = graph.migrations["a4c8e2f61b93"].enum_extensions[0]
    assert (ext.enum_name, ext.added_values, ext.columns) == (
        "tool_category",
        ["send_sms"],
        [("tools", "category")],
    )


def test_upgrade_path_through_merge_orders_parents_first():
    graph = load_graph(
        [
            mig("a", None),
            mig("b", "a"),
            mig("c", "a"),
            mig("d", ("b", "c")),
            mig("e", "d"),
        ]
    )
    assert graph.heads == ["e"]
    assert graph.upgrade_path("b", "e") == ["c", "d", "e"]
    assert graph.upgrade_path("a", "e") == ["b", "c", "d", "e"]


@pytest.mark.parametrize(
    "files,message",
    [
        ([mig("a", None), mig("b", "a"), mig("c", "a")], "exactly one head"),
        ([mig("a", None), mig("b", None)], "one base revision"),
        ([mig("a", None), mig("b", "zzz")], "unknown parent"),
        ([mig("a", None), mig("a", None)], "duplicate revision"),
        ([mig("a", "b"), mig("b", "a"), mig("c", None)], "cycle"),
        ([mig("a", None, extra='branch_labels = ("x",)')], "branch labels"),
        ([("x.py", "revision = make()\n")], "not a literal"),
        ([("x.py", "import os\n")], "no revision identifier"),
        ([], "no migrations"),
    ],
)
def test_invalid_graphs_are_rejected(files, message):
    with pytest.raises(GraphError, match=message):
        load_graph(files)


@pytest.mark.parametrize(
    "body,risk",
    [
        ('op.add_column("t", sa.Column("c", sa.Text(), nullable=True))', "additive"),
        ('op.create_index("i", "t", ["c"])', "additive"),
        ('op.add_column("t", sa.Column("c", sa.Text(), nullable=False))', "review"),
        (
            'op.add_column("t", sa.Column("c", sa.Text(), nullable=False, server_default="x"))',
            "additive",
        ),
        ('op.drop_column("t", "c")', "review"),
        ('op.alter_column("t", "c", nullable=False)', "review"),
        ('op.execute("UPDATE t SET c = 1")', "review"),
        (
            'op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c)")',
            "additive",
        ),
        ('op.execute(f"DROP TABLE {name}")', "review"),
    ],
)
def test_migration_risk_classification(body, risk):
    graph = load_graph([mig("a", None, body)])
    assert graph.migrations["a"].risk == risk


def test_enum_value_removal_is_not_an_extension():
    up = 'op.sync_enum_values(enum_schema="public", enum_name="e", new_values=["x"], affected_columns=[], enum_values_to_rename=[])'
    src = (
        'revision = "a"\ndown_revision = None\nfrom alembic import op\n'
        f"def upgrade():\n    {up}\n"
        'def downgrade():\n    op.sync_enum_values(enum_schema="public", enum_name="e", new_values=["x", "y"], '
        "affected_columns=[], enum_values_to_rename=[])\n"
    )
    graph = load_graph([("a.py", src)])
    assert graph.migrations["a"].risk == "review"


def test_parsing_never_imports_migration_code(tmp_path):
    marker = tmp_path / "ran"
    src = f'import pathlib\npathlib.Path("{marker}").write_text("x")\nrevision = "a"\ndown_revision = None\n'
    load_graph([("a.py", src)])
    assert not marker.exists()
