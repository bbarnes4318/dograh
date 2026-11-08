"""Owner-only agents: hidden from everybody in the shared workspace but the
platform owner (api/services/auth/viewer.py)."""

import contextvars
from pathlib import Path

from sqlalchemy.dialects import postgresql

from api.services.auth import viewer

API = Path(__file__).resolve().parents[1]


def _sql(clause) -> str:
    return str(
        clause.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _in_fresh_context(fn):
    return contextvars.Context().run(fn)


def test_only_an_explicit_true_claim_is_the_owner():
    assert viewer.token_is_platform_owner({"platform_owner": True})
    for payload in (
        {},
        {"platform_owner": False},
        {"platform_owner": "true"},
        {"platform_owner": 1},
        {"platform_owner": None},
    ):
        assert not viewer.token_is_platform_owner(payload), payload


def test_unauthenticated_contexts_see_everything():
    # API keys, telephony webhooks, the call runtime and workers never call
    # set_viewer_from_token: they must keep reaching owner-only agents.
    assert _in_fresh_context(viewer.owner_only_hidden) is False
    assert _in_fresh_context(lambda: _sql(viewer.visible_workflows())) == "true"


def test_a_token_without_the_claim_hides_owner_only_agents():
    def run():
        viewer.set_viewer_from_token({"sub": "1", "email": "someone@example.com"})
        return viewer.owner_only_hidden(), _sql(viewer.workflow_in_org(42))

    hidden, sql = _in_fresh_context(run)
    assert hidden is True
    assert "workflows.organization_id = 42" in sql
    assert "workflows.owner_only IS false" in sql


def test_the_owner_token_sees_owner_only_agents():
    def run():
        viewer.set_viewer_from_token({"sub": "1", "platform_owner": True})
        return viewer.owner_only_hidden(), _sql(viewer.workflow_in_org(42))

    hidden, sql = _in_fresh_context(run)
    assert hidden is False
    assert "owner_only" not in sql
    assert "workflows.organization_id = 42" in sql


def test_requests_do_not_leak_into_each_other():
    def restricted():
        viewer.set_viewer_from_token({})
        return viewer.owner_only_hidden()

    assert _in_fresh_context(restricted) is True
    assert _in_fresh_context(viewer.owner_only_hidden) is False


def test_user_facing_workflow_queries_all_go_through_the_filter():
    # The only plain org checks left on workflows are the two dialing
    # safeguards, which must count every agent's calls.
    leftovers = {}
    for path in (API / "db").glob("*.py"):
        n = path.read_text().count("WorkflowModel.organization_id == organization_id")
        if n:
            leftovers[path.name] = n
    assert leftovers == {"workflow_run_client.py": 2}


def test_the_migration_is_purely_additive_for_the_cutover_planner():
    import sys

    sys.path.insert(0, str(API.parent / "deploy" / "hopwhistle" / "cutover"))
    from cutover_lib.migrations import parse_migration

    path = API / "alembic" / "versions" / "e4b7a2c9d013_add_workflow_owner_only.py"
    migration = parse_migration(path.read_text(), path.name)
    assert migration is not None
    assert migration.risk == "additive", migration.risk_reasons


async def _oss_auth_hides(monkeypatch, payload: dict) -> bool:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api.services.auth import depends as auth_depends

    monkeypatch.setattr(auth_depends, "decode_jwt_token", lambda _t: payload)
    monkeypatch.setattr(
        auth_depends.db_client,
        "get_user_by_id",
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    await auth_depends._handle_oss_auth("Bearer t")
    return viewer.owner_only_hidden()


async def test_email_password_auth_narrows_a_non_owner(monkeypatch):
    assert await _oss_auth_hides(monkeypatch, {"sub": "1", "email": "a@b.c"})


async def test_email_password_auth_leaves_the_owner_unrestricted(monkeypatch):
    payload = {"sub": "1", "email": "a@b.c", "platform_owner": True}
    assert not await _oss_auth_hides(monkeypatch, payload)
