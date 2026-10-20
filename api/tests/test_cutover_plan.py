"""Cutover plan: every reason to refuse a switch, decided before anything
changes, and a plan that never prints secrets or phone numbers."""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CUTOVER = REPO / "deploy" / "hopwhistle" / "cutover"
sys.path.insert(0, str(CUTOVER))

from cutover_lib import compose
from cutover_lib.migrations import load_graph, load_graph_from_dir
from cutover_lib.mounts import evaluate, load_manifest
from cutover_lib.plan import PlanInputs, build_plan, fingerprint, redact, render

MANIFEST = load_manifest(str(CUTOVER / "mount_manifest.json"))
REAL = load_graph_from_dir(str(REPO / "api" / "alembic" / "versions"))
OLD_HEAD = "00b0201ad918"


def graph_files(graph, head):
    files = []
    for rev in graph.ancestors(head):
        m = graph.migrations[rev]
        down = (
            m.down_revisions[0]
            if len(m.down_revisions) == 1
            else (m.down_revisions or None)
        )
        files.append((m.filename, f"revision = {rev!r}\ndown_revision = {down!r}\n"))
    return files


def subgraph(graph, head):
    return load_graph(graph_files(graph, head))


OLD = subgraph(REAL, OLD_HEAD)

OVERRIDE = """\
services:
  api:
    image: dograhai/dograh-api:latest
    volumes:
      - /opt/dograh-patches/d.py:/app/api/services/campaign/campaign_call_dispatcher.py:ro
    environment:
      FISH_API_KEY: "fish-secret-value"
"""
DISPATCHER = 'NO_CALLER_ID_AVAILABLE_FOR_DESTINATION_STATE\nSHARED_TRANSFER_DID = os.environ.get("DOGRAH_TRANSFER_DESTINATION") or "+15550001234"\n'


def image_files():
    out = {}
    for b in MANIFEST["behaviors"].values():
        for ev in b["evidence"]:
            local = REPO / ev["path"].removeprefix("/app/")
            out[ev["path"]] = local.read_text() if local.exists() else None
    return out


def mounts_eval(override=OVERRIDE, host=None):
    svc = compose.parse(override).services["api"]
    host = host or {"/opt/dograh-patches/d.py": DISPATCHER}
    return evaluate(MANIFEST, svc.volumes, host.get, image_files(), {}, {}, {})


def inputs(**kw):
    base = {
        "mode": "cutover",
        "service": "api",
        "target_image": "hopwhistle/dograh-api:abc12345",
        "target_image_id": "sha256:" + "a" * 64,
        "target_graph": REAL,
        "target_graph_error": None,
        "running_image_id": "sha256:" + "b" * 64,
        "running_graph": OLD,
        "running_graph_error": None,
        "db_revisions": [OLD_HEAD],
        "override_text": OVERRIDE,
        "mounts": mounts_eval(),
    }
    base.update(kw)
    return PlanInputs(**base)


def test_happy_path_plan_derives_head_from_the_image():
    plan = build_plan(inputs())
    assert plan.ok, plan.blockers
    assert plan.data["migrations"]["from"] == OLD_HEAD
    assert plan.data["migrations"]["to"] == REAL.heads[0]
    assert [
        p["revision"] for p in plan.data["migrations"]["pending"]
    ] == REAL.upgrade_path(OLD_HEAD, REAL.heads[0])
    assert plan.data["recovery"]["image_rollback_without_schema_change"] is False
    assert "b7c41d9e52aa" in plan.data["recovery"]["non_transactional_pending"]
    new = compose.parse(plan.override_text).services["api"]
    assert new.image == "hopwhistle/dograh-api:abc12345"
    assert new.volumes == []
    assert new.env["CAMPAIGN_TRANSFER_DESTINATION_POLICY"] == "forced_default"
    assert (
        new.env["FISH_API_KEY"] == "fish-secret-value"
    )  # copied through, never printed


def test_up_to_date_database_has_no_pending_migrations():
    plan = build_plan(inputs(running_graph=REAL, db_revisions=[REAL.heads[0]]))
    assert plan.ok and plan.data["migrations"]["pending"] == []
    assert plan.data["recovery"]["image_rollback_without_schema_change"] is True


def test_database_not_on_running_image_head_blocks():
    plan = build_plan(inputs(db_revisions=["c52dc3cccfb0"]))
    assert any("not in the state its own code expects" in b for b in plan.blockers)
    assert plan.override_text is None


def test_database_ahead_of_or_diverged_from_target_blocks():
    plan = build_plan(inputs(db_revisions=["ffffffffffff"]))
    assert any("unknown to the target image" in b for b in plan.blockers)


def test_multiple_alembic_rows_block():
    plan = build_plan(inputs(db_revisions=[OLD_HEAD, "b7c41d9e52aa"]))
    assert any("divergent heads" in b for b in plan.blockers)


def test_missing_or_empty_alembic_version_blocks():
    assert any("missing" in b for b in build_plan(inputs(db_revisions=None)).blockers)
    assert any("empty" in b for b in build_plan(inputs(db_revisions=[])).blockers)


def test_target_with_multiple_heads_blocks():
    plan = build_plan(
        inputs(
            target_graph=None,
            target_graph_error="expected exactly one head, found ['x', 'y']",
        )
    )
    assert any("exactly one head" in b for b in plan.blockers)


def test_target_that_is_not_a_descendant_blocks():
    files = graph_files(REAL, OLD_HEAD)
    files.append(("zz.py", f"revision = 'zz'\ndown_revision = {OLD_HEAD!r}\n"))
    running = load_graph(files)
    plan = build_plan(inputs(running_graph=running, db_revisions=["zz"]))
    assert any(
        "not a descendant" in b or "unknown to the target" in b for b in plan.blockers
    )


def test_too_many_pending_migrations_blocks():
    plan = build_plan(inputs(max_pending=2))
    assert any("exceeds the supported maximum" in b for b in plan.blockers)


def test_non_additive_pending_migration_needs_explicit_allow():
    old = subgraph(REAL, "c52dc3cccfb0")
    plan = build_plan(inputs(running_graph=old, db_revisions=["c52dc3cccfb0"]))
    review = [p for p in plan.data["migrations"]["pending"] if p["risk"] == "review"]
    assert review, "00b0201ad918 backfills data and must be flagged"
    assert any("--allow-migration" in b for b in plan.blockers)
    allowed = build_plan(
        inputs(
            running_graph=old,
            db_revisions=["c52dc3cccfb0"],
            allow_migrations={p["revision"] for p in review},
        )
    )
    assert allowed.ok, allowed.blockers
    assert allowed.data["recovery"]["stamp_back_possible_after_migration"] is False


def test_other_service_mounting_over_api_blocks():
    override = (
        OVERRIDE + "  worker:\n    volumes:\n      - /opt/x.py:/app/api/x.py:ro\n"
    )
    plan = build_plan(inputs(override_text=override, mounts=mounts_eval(override)))
    assert any("service worker also mounts over /app/api" in b for b in plan.blockers)


def test_unknown_mount_blocks_the_whole_plan():
    override = OVERRIDE.replace(
        "campaign/campaign_call_dispatcher.py", "telephony/ari_manager.py"
    )
    plan = build_plan(inputs(override_text=override, mounts=mounts_eval(override)))
    assert not plan.ok and plan.override_text is None


def test_plan_output_is_redacted():
    plan = build_plan(inputs())
    text = render(plan.data) + json.dumps(plan.data)
    assert "+15550001234" not in text and "15550001234" not in text
    assert "fish-secret-value" not in text
    assert (
        plan.data["environment"]["CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"]["value"]
        == "<set>"
    )
    assert (
        plan.data["environment"]["CAMPAIGN_TRANSFER_DESTINATION_POLICY"]["value"]
        == "forced_default"
    )


def test_redact_keeps_hashes_and_removes_numbers():
    digest = "1234567890" + "a" * 54
    assert redact(f"sha {digest}") == f"sha {digest}"
    assert redact("call +15551234567 now") == "call <number> now"
    assert redact("x=5551234567") == "x=<number>"


def test_fingerprint_changes_with_any_input():
    base = fingerprint("o", ["a"], "t", "r")
    assert base != fingerprint("o2", ["a"], "t", "r")
    assert base != fingerprint("o", ["b"], "t", "r")
    assert base != fingerprint("o", ["a"], "t2", "r")
    assert base != fingerprint("o", ["a"], "t", "r2")
