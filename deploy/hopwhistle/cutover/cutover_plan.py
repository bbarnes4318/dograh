#!/usr/bin/env python3
"""Plan, check and recover the hopwhistle api cutover. Read-only except for
files it writes into --state-dir.

    cutover_plan.py graph  --image IMG
    cutover_plan.py plan   --compose-dir D --target-image IMG --state-dir S \\
                           [--mode cutover|update] [--ack-mount TARGET=SHA256]... \\
                           [--set NAME=VALUE]... [--allow-migration REV]... [--max-pending N]
    cutover_plan.py diff   --compose-dir D --target-image IMG TARGET
    cutover_plan.py decide --compose-dir D --state-dir S
    cutover_plan.py verify --compose-dir D --state-dir S

Exit codes: 0 ok, 2 error, 3 plan blocked / verification failed.
Nothing here starts, stops or recreates a container, and every database
query runs in a READ ONLY transaction.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from cutover_lib import compose, recovery
from cutover_lib.io import Docker, Psql
from cutover_lib.migrations import GraphError, load_graph
from cutover_lib.mounts import READ_ENV, evaluate, load_manifest
from cutover_lib.plan import PlanInputs, build_plan, redact, render

VERSIONS_DIR = "/app/api/alembic/versions"
EXIT_BLOCKED = 3
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _pairs(values: list[str], what: str) -> dict[str, str]:
    out = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"{what} expects NAME=VALUE, got {item!r}")
        out[key] = value
    return out


def _psql(compose_dir: str) -> Psql:
    raw = os.environ.get("CUTOVER_PSQL_CMD")
    if not raw:
        raise SystemExit("CUTOVER_PSQL_CMD is not set (common.sh sets it)")
    return Psql(json.loads(raw), cwd=compose_dir)


def _api_container(compose_dir: str, service: str, docker: Docker) -> str:
    cid = docker.compose_container(compose_dir, service)
    if not cid:
        raise SystemExit(f"no {service} container in {compose_dir}")
    return cid


def _write(path: str, text: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _save_graph(state_dir: str, name: str, files: list[tuple[str, str]]) -> None:
    folder = os.path.join(state_dir, "graphs", name)
    os.makedirs(folder, exist_ok=True)
    for filename, source in files:
        _write(os.path.join(folder, os.path.basename(filename)), source, 0o644)


def _load_saved_graph(state_dir: str, name: str):
    folder = os.path.join(state_dir, "graphs", name)
    files = []
    for filename in sorted(os.listdir(folder)):
        with open(os.path.join(folder, filename), encoding="utf-8") as handle:
            files.append((filename, handle.read()))
    return load_graph(files)


def _graph(files):
    try:
        return load_graph(files), None
    except GraphError as exc:
        return None, str(exc)


def _manifest_paths(manifest: dict) -> list[str]:
    paths = set(manifest["mounts"])
    for behavior in manifest["behaviors"].values():
        for ev in behavior.get("evidence", []):
            paths.add(ev["path"])
    return sorted(paths)


def cmd_graph(args) -> int:
    docker = Docker()
    files = docker.read_dir(args.image, VERSIONS_DIR, is_image=True)
    graph, error = _graph(files)
    if graph is None:
        print(f"INVALID: {error}")
        return EXIT_BLOCKED
    print(
        json.dumps(
            {
                "image": args.image,
                "head": graph.heads[0],
                "revisions": len(graph.migrations),
            }
        )
    )
    return 0


def cmd_plan(args) -> int:
    docker = Docker()
    os.makedirs(args.state_dir, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    manifest = load_manifest(args.manifest)
    container = _api_container(args.compose_dir, args.service, docker)
    override_path = os.path.join(args.compose_dir, "docker-compose.override.yaml")
    with open(override_path, encoding="utf-8") as handle:
        override_text = handle.read()

    target_id = docker.image_id(args.target_image)
    running_id = docker.container_image_id(container)
    target_files = docker.read_dir(args.target_image, VERSIONS_DIR, is_image=True)
    running_files = docker.read_dir(container, VERSIONS_DIR, is_image=False)
    target_graph, target_error = _graph(target_files)
    running_graph, running_error = _graph(running_files)
    _save_graph(args.state_dir, "target", target_files)
    _save_graph(args.state_dir, "previous", running_files)

    db_revisions = _psql(args.compose_dir).alembic_revisions()
    container_env = docker.container_env(container, READ_ENV)

    mounts_eval = None
    try:
        parsed = compose.parse(override_text)
        svc = parsed.services.get(args.service)
    except compose.OverrideError:
        svc = None
    if svc is not None:
        image_files = docker.read_paths(
            args.target_image, _manifest_paths(manifest), is_image=True
        )

        def read_host(path: str):
            try:
                with open(path, encoding="utf-8") as handle:
                    return handle.read()
            except OSError:
                return None

        mounts_eval = evaluate(
            manifest,
            svc.volumes,
            read_host,
            image_files,
            container_env,
            _pairs(args.ack_mount, "--ack-mount"),
            _pairs(args.set, "--set"),
        )

    plan = build_plan(
        PlanInputs(
            mode=args.mode,
            service=args.service,
            target_image=args.target_image,
            target_image_id=target_id,
            target_graph=target_graph,
            target_graph_error=target_error,
            running_image_id=running_id,
            running_graph=running_graph,
            running_graph_error=running_error,
            db_revisions=db_revisions,
            override_text=override_text,
            mounts=mounts_eval,
            max_pending=args.max_pending,
            allow_migrations=set(args.allow_migration or []),
        )
    )
    plan.data["running"]["container"] = container[:12]
    text = render(plan.data)
    _write(
        os.path.join(args.state_dir, "plan.json"),
        redact(json.dumps(plan.data, indent=2)) + "\n",
        0o644,
    )
    _write(os.path.join(args.state_dir, "plan.txt"), text, 0o644)
    if plan.override_text is not None:
        _write(
            os.path.join(args.state_dir, "override.new.yaml"), plan.override_text, 0o600
        )
    state = {
        "phase": "planned",
        "mode": args.mode,
        "service": args.service,
        "pre_revision": plan.data["migrations"]["from"],
        "target_head": plan.data["migrations"]["to"],
        "target_image": args.target_image,
        "target_image_id": target_id,
        "previous_image_id": running_id,
        "fingerprint": plan.data["fingerprint"],
        "removed_mounts": [
            m["target"] for m in plan.data.get("mounts", []) if m["action"] == "remove"
        ],
        "env_names": sorted(plan.data.get("environment", {})),
        "new_image_was_healthy": False,
    }
    _write(
        os.path.join(args.state_dir, "state.json"),
        json.dumps(state, indent=2) + "\n",
        0o600,
    )
    sys.stdout.write(text)
    return 0 if plan.ok else EXIT_BLOCKED


def cmd_fingerprint(args) -> int:
    """Re-collect the planned-against state and compare (TOCTOU guard)."""
    from cutover_lib.plan import fingerprint

    docker = Docker()
    state = _state(args.state_dir)
    container = _api_container(args.compose_dir, state["service"], docker)
    with open(
        os.path.join(args.compose_dir, "docker-compose.override.yaml"), encoding="utf-8"
    ) as handle:
        override_text = handle.read()
    now = fingerprint(
        override_text,
        _psql(args.compose_dir).alembic_revisions(),
        docker.image_id(state["target_image"]),
        docker.container_image_id(container),
    )
    if now != state["fingerprint"]:
        print(
            "CHANGED: the override, database revision, target image or running image changed since the plan"
        )
        return EXIT_BLOCKED
    print("unchanged")
    return 0


def cmd_diff(args) -> int:
    docker = Docker()
    with open(
        os.path.join(args.compose_dir, "docker-compose.override.yaml"), encoding="utf-8"
    ) as handle:
        parsed = compose.parse(handle.read())
    svc = parsed.services[args.service]
    mount = next((v for v in svc.volumes if v.target == args.target), None)
    if mount is None:
        raise SystemExit(f"{args.target} is not mounted on {args.service}")
    with open(mount.source, encoding="utf-8") as handle:
        mounted = handle.read()
    image = (
        docker.read_paths(args.target_image, [args.target], is_image=True)[args.target]
        or ""
    )
    diff = difflib.unified_diff(
        image.splitlines(),
        mounted.splitlines(),
        fromfile=f"target-image:{args.target}",
        tofile=f"mounted:{mount.source}",
        lineterm="",
    )
    for line in diff:
        print(redact(line))
    return 0


def _state(state_dir: str) -> dict:
    with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _container_class(docker: Docker, compose_dir: str, service: str) -> str:
    try:
        cid = _api_container(compose_dir, service, docker)
        st = docker.container_state(cid)
    except (SystemExit, RuntimeError):
        return "missing"
    if st["status"] == "running":
        return "healthy" if st["health"] == "healthy" else "starting"
    if st["status"] == "restarting":
        return "restarting"
    if st["status"] in ("exited", "dead", "created"):
        return "exited"
    return "unknown"


def _enum_rows(psql: Psql, graph, revisions: list[str]):
    total = 0
    for rev in revisions:
        for ext in graph.migrations[rev].enum_extensions:
            values = [v for v in ext.added_values if _IDENT.match(v)]
            for table, column in ext.columns:
                if not (_IDENT.match(table) and _IDENT.match(column)) or not values:
                    return None
                quoted = ", ".join(f"'{v}'" for v in values)
                total += psql.count(
                    f'SELECT count(*) FROM "{table}" WHERE "{column}"::text IN ({quoted})'
                )
    return total


def gather_facts(args):
    docker = Docker()
    state = _state(args.state_dir)
    psql = _psql(args.compose_dir)
    try:
        db = psql.alembic_revisions()
        db = db if db is not None else []
    except RuntimeError:
        db = None
    new_graph = _load_saved_graph(args.state_dir, "target")
    old_graph = _load_saved_graph(args.state_dir, "previous")
    facts = recovery.Facts(
        phase=state["phase"],
        container=_container_class(docker, args.compose_dir, state["service"]),
        db_revisions=db,
        pre_revision=state["pre_revision"],
        target_head=state["target_head"],
        old_graph=old_graph,
        new_graph=new_graph,
        new_image_was_healthy=state.get("new_image_was_healthy", False),
    )
    try:
        facts.invalid_indexes = psql.invalid_indexes()
        facts.rows_with_new_enum_values = _enum_rows(
            psql, new_graph, recovery.forward_revisions(facts)
        )
    except RuntimeError:
        facts.rows_with_new_enum_values = None
    return state, facts


def cmd_decide(args) -> int:
    _, facts = gather_facts(args)
    decision = recovery.decide(facts)
    out = {
        "action": decision.action,
        "automatic": decision.automatic,
        "summary": decision.summary,
        "reasons": decision.reasons,
        "options": decision.options,
        "facts": {
            "phase": facts.phase,
            "container": facts.container,
            "db_revisions": facts.db_revisions,
            "pre_revision": facts.pre_revision,
            "target_head": facts.target_head,
            "forward_revisions": recovery.forward_revisions(facts),
            "invalid_indexes": facts.invalid_indexes,
            "rows_with_new_enum_values": facts.rows_with_new_enum_values,
            "old_image_compatible": recovery.old_image_compatible(facts)[0],
            "stamp_back_allowed": recovery.stamp_back_check(facts)[0],
        },
    }
    print(redact(json.dumps(out, indent=2)))
    return 0


def cmd_check(args) -> int:
    """Preconditions for one recover.sh choice; exit 0 when allowed."""
    _, facts = gather_facts(args)
    if args.choice == "image-rollback":
        ok, reasons = recovery.old_image_compatible(facts)
    elif args.choice == "stamp-old-head":
        ok, reasons = recovery.stamp_back_check(facts)
    elif args.choice == "resume-forward":
        reasons = []
        if facts.db_revisions is None or any(
            r not in facts.new_graph.migrations for r in facts.db_revisions
        ):
            reasons.append("database revision is unknown to the new image")
        if facts.invalid_indexes:
            reasons.append(
                f"drop invalid indexes first: {', '.join(facts.invalid_indexes)}"
            )
        ok = not reasons
    else:
        raise SystemExit(f"unknown choice {args.choice}")
    print(
        ("ALLOWED" if ok else "REFUSED")
        + "".join(f"\n  - {redact(r)}" for r in reasons)
    )
    return 0 if ok else EXIT_BLOCKED


def cmd_verify(args) -> int:
    docker = Docker()
    state = _state(args.state_dir)
    container = _api_container(args.compose_dir, state["service"], docker)
    problems = []
    db = _psql(args.compose_dir).alembic_revisions()
    if db != [state["target_head"]]:
        problems.append(f"database on {db}, expected [{state['target_head']}]")
    if docker.container_image_id(container) != state["target_image_id"]:
        problems.append("api is not running the planned image")
    mounted = set(docker.container_mount_targets(container))
    for target in state["removed_mounts"]:
        if target in mounted:
            problems.append(f"{target} is still mounted")
    env = docker.container_env(container, state["env_names"])
    with open(
        os.path.join(args.state_dir, "override.new.yaml"), encoding="utf-8"
    ) as handle:
        svc = compose.parse(handle.read()).services[state["service"]]
    for name in state["env_names"]:
        if env.get(name) != svc.env.get(name):
            problems.append(f"{name} in the container does not match the override")
    for p in problems:
        print(f"VERIFY FAILED: {redact(p)}")
    if not problems:
        print(
            f"verified: image {state['target_image']}, database {db}, {len(state['removed_mounts'])} mounts removed"
        )
    return 0 if not problems else EXIT_BLOCKED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("graph")
    g.add_argument("--image", required=True)

    def common(p, state=True):
        p.add_argument(
            "--compose-dir", default=os.environ.get("COMPOSE_DIR", "/opt/dograh")
        )
        p.add_argument("--service", default="api")
        if state:
            p.add_argument("--state-dir", required=True)

    p = sub.add_parser("plan")
    common(p)
    p.add_argument("--target-image", required=True)
    p.add_argument("--mode", choices=("cutover", "update"), default="cutover")
    p.add_argument("--manifest", default=os.path.join(HERE, "mount_manifest.json"))
    p.add_argument("--ack-mount", action="append", default=[])
    p.add_argument("--set", action="append", default=[])
    p.add_argument("--allow-migration", action="append", default=[])
    p.add_argument("--max-pending", type=int, default=20)

    f = sub.add_parser("fingerprint")
    common(f)

    d = sub.add_parser("diff")
    common(d, state=False)
    d.add_argument("--target-image", required=True)
    d.add_argument("target")

    for name in ("decide", "verify"):
        common(sub.add_parser(name))
    c = sub.add_parser("check")
    common(c)
    c.add_argument(
        "choice", choices=("image-rollback", "stamp-old-head", "resume-forward")
    )

    args = parser.parse_args(argv)
    handlers = {
        "graph": cmd_graph,
        "plan": cmd_plan,
        "fingerprint": cmd_fingerprint,
        "diff": cmd_diff,
        "decide": cmd_decide,
        "verify": cmd_verify,
        "check": cmd_check,
    }
    try:
        return handlers[args.cmd](args)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
