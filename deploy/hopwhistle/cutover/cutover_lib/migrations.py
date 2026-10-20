"""Alembic revision graphs read statically from migration files.

The files are parsed with ``ast`` and never imported or executed, so reading
an image's graph cannot touch a database or run its startup migration.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Operations whose effect old application code can live with: new columns it
# never selects, new indexes, new tables.
_ADDITIVE_OPS = {"add_column", "create_index", "create_table"}
_ADDITIVE_SQL = re.compile(
    r"^\s*(ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+\w+\s+\w+\s*$"
    r"|CREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(IF\s+NOT\s+EXISTS\s+)?)",
    re.IGNORECASE,
)


class GraphError(ValueError):
    """The migration files do not form a graph we can reason about."""


@dataclass
class EnumExtension:
    enum_name: str
    added_values: list[str]
    columns: list[tuple[str, str]]  # (table, column)


@dataclass
class Migration:
    revision: str
    down_revisions: tuple[str, ...]
    filename: str
    depends_on: tuple[str, ...] = ()
    branch_labels: tuple[str, ...] = ()
    non_transactional: bool = False
    risk: str = "review"  # additive | enum_extension | review
    risk_reasons: list[str] = field(default_factory=list)
    enum_extensions: list[EnumExtension] = field(default_factory=list)


@dataclass
class RevisionGraph:
    migrations: dict[str, Migration]

    @property
    def heads(self) -> list[str]:
        parents = {p for m in self.migrations.values() for p in m.down_revisions}
        return sorted(r for r in self.migrations if r not in parents)

    @property
    def roots(self) -> list[str]:
        return sorted(r for r, m in self.migrations.items() if not m.down_revisions)

    def ancestors(self, revision: str) -> set[str]:
        """``revision`` and everything it depends on."""
        seen: set[str] = set()
        stack = [revision]
        while stack:
            rev = stack.pop()
            if rev in seen:
                continue
            seen.add(rev)
            m = self.migrations[rev]
            stack.extend(m.down_revisions)
            stack.extend(m.depends_on)
        return seen

    def upgrade_path(self, current: str, target: str) -> list[str]:
        """Revisions alembic would apply going from ``current`` to ``target``,
        parents before children."""
        pending = self.ancestors(target) - self.ancestors(current)
        ordered: list[str] = []
        placed: set[str] = set()
        while len(ordered) < len(pending):
            progressed = False
            for rev in sorted(pending - placed):
                m = self.migrations[rev]
                deps = set(m.down_revisions) | set(m.depends_on)
                if deps & pending <= placed:
                    ordered.append(rev)
                    placed.add(rev)
                    progressed = True
            if not progressed:  # validate() rules cycles out; be safe anyway
                raise GraphError("cannot order pending migrations")
        return ordered


def _literal(node: ast.AST, filename: str, name: str):
    try:
        value = ast.literal_eval(node)
    except ValueError as exc:
        raise GraphError(f"{filename}: {name} is not a literal") from exc
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise GraphError(f"{filename}: unexpected {name} value {value!r}")


def _module_values(tree: ast.Module, filename: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in (
            "revision",
            "down_revision",
            "depends_on",
            "branch_labels",
        ):
            out[target.id] = _literal(value, filename, target.id)
    return out


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _op_calls(func: ast.FunctionDef) -> list[ast.Call]:
    calls = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        ):
            calls.append(node)
    return calls


def _sql_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "X" for v in node.values
        )
    return None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _enum_values(call: ast.Call) -> list[str] | None:
    node = _kwarg(call, "new_values")
    if node is None:
        return None
    try:
        values = ast.literal_eval(node)
    except ValueError:
        return None
    return list(values) if isinstance(values, (list, tuple)) else None


def _enum_columns(call: ast.Call) -> list[tuple[str, str]]:
    columns = []
    node = _kwarg(call, "affected_columns")
    for item in getattr(node, "elts", []) or []:
        if isinstance(item, ast.Call):
            table = _kwarg(item, "table_name")
            column = _kwarg(item, "column_name")
            if isinstance(table, ast.Constant) and isinstance(column, ast.Constant):
                columns.append((table.value, column.value))
    return columns


def _column_is_unsafe_for_old_code(call: ast.Call) -> bool:
    # add_column(table, sa.Column(name, type, nullable=False)) without a
    # server default makes every insert by old code fail.
    for arg in call.args:
        if isinstance(arg, ast.Call):
            nullable = _kwarg(arg, "nullable")
            default = _kwarg(arg, "server_default")
            if (
                isinstance(nullable, ast.Constant)
                and nullable.value is False
                and default is None
            ):
                return True
    return False


def classify(tree: ast.Module, migration: Migration) -> None:
    upgrade = _function(tree, "upgrade")
    downgrade = _function(tree, "downgrade")
    if upgrade is None:
        migration.risk, migration.risk_reasons = "review", ["no upgrade()"]
        return
    reasons: list[str] = []
    enum_ext: list[EnumExtension] = []
    down_enums: dict[str, list[str]] = {}
    if downgrade is not None:
        for call in _op_calls(downgrade):
            if call.func.attr == "sync_enum_values":
                name = _kwarg(call, "enum_name")
                values = _enum_values(call)
                if isinstance(name, ast.Constant) and values is not None:
                    down_enums[name.value] = values

    for call in _op_calls(upgrade):
        name = call.func.attr
        if name == "get_context":
            continue
        if name in _ADDITIVE_OPS:
            if name == "add_column" and _column_is_unsafe_for_old_code(call):
                reasons.append("add_column NOT NULL without server_default")
            continue
        if name == "execute":
            sql = _sql_text(call.args[0]) if call.args else None
            if sql is None or not _ADDITIVE_SQL.match(sql):
                reasons.append("op.execute with non-additive or dynamic SQL")
            continue
        if name == "sync_enum_values":
            enum = _kwarg(call, "enum_name")
            values = _enum_values(call)
            old = down_enums.get(enum.value) if isinstance(enum, ast.Constant) else None
            if values is None or old is None or not set(old) <= set(values):
                reasons.append("enum change that is not a pure value addition")
                continue
            renames = _kwarg(call, "enum_values_to_rename")
            if renames is not None and getattr(renames, "elts", None):
                reasons.append("enum value rename")
                continue
            enum_ext.append(
                EnumExtension(
                    enum.value,
                    [v for v in values if v not in old],
                    _enum_columns(call),
                )
            )
            continue
        reasons.append(f"op.{name}")

    src = ast.dump(upgrade)
    migration.non_transactional = "autocommit_block" in src
    migration.enum_extensions = enum_ext
    migration.risk_reasons = reasons
    if reasons:
        migration.risk = "review"
    elif enum_ext:
        migration.risk = "enum_extension"
    else:
        migration.risk = "additive"


def parse_migration(source: str, filename: str) -> Migration | None:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise GraphError(f"{filename}: {exc}") from exc
    values = _module_values(tree, filename)
    if "revision" not in values:
        return None
    revision = values["revision"]
    if len(revision) != 1:
        raise GraphError(f"{filename}: revision must be one string")
    migration = Migration(
        revision=revision[0],
        down_revisions=values.get("down_revision", ()),
        filename=os.path.basename(filename),
        depends_on=values.get("depends_on", ()),
        branch_labels=values.get("branch_labels", ()),
    )
    classify(tree, migration)
    return migration


def load_graph(files: Iterable[tuple[str, str]]) -> RevisionGraph:
    """Build a graph from (filename, source) pairs and validate it."""
    migrations: dict[str, Migration] = {}
    for filename, source in files:
        if not filename.endswith(".py") or os.path.basename(filename).startswith("__"):
            continue
        m = parse_migration(source, filename)
        if m is None:
            raise GraphError(f"{filename}: no revision identifier")
        if m.revision in migrations:
            raise GraphError(
                f"duplicate revision {m.revision} in {m.filename} and "
                f"{migrations[m.revision].filename}"
            )
        migrations[m.revision] = m
    graph = RevisionGraph(migrations)
    validate(graph)
    return graph


def load_graph_from_dir(path: str) -> RevisionGraph:
    files = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full) and name.endswith(".py"):
            with open(full, encoding="utf-8") as handle:
                files.append((name, handle.read()))
    return load_graph(files)


def validate(graph: RevisionGraph) -> None:
    if not graph.migrations:
        raise GraphError("no migrations found")
    for m in graph.migrations.values():
        for parent in (*m.down_revisions, *m.depends_on):
            if parent not in graph.migrations:
                raise GraphError(f"{m.filename}: unknown parent revision {parent}")
        if m.branch_labels:
            raise GraphError(f"{m.filename}: branch labels are not supported")
    # Cycle check (iterative DFS, colours).
    state: dict[str, int] = {}
    for start in graph.migrations:
        if state.get(start):
            continue
        stack = [(start, iter(graph.migrations[start].down_revisions))]
        state[start] = 1
        while stack:
            rev, children = stack[-1]
            nxt = next(children, None)
            if nxt is None:
                state[rev] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                raise GraphError(f"cycle through {nxt}")
            elif not state.get(nxt):
                state[nxt] = 1
                stack.append((nxt, iter(graph.migrations[nxt].down_revisions)))
    if len(graph.roots) != 1:
        raise GraphError(f"expected one base revision, found {graph.roots}")
    if len(graph.heads) != 1:
        raise GraphError(f"expected exactly one head, found {graph.heads}")
