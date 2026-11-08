"""Who is looking: whether this request may see owner-only agents.

Every Hopwhistle user who opens AI Voice is signed in as the SAME Dograh user
(Hopwhistle's routes/aivoice.ts mints the token with one shared user id), so
the user row cannot tell the platform owner from anybody else. The token can:
Hopwhistle signs it with this deployment's secret and sets
``platform_owner: true`` only for the people named in its
PLATFORM_OWNER_EMAILS.

An agent with ``workflows.owner_only = true`` is visible only to such a token.
Everybody else signed in with an email/password token, from Hopwhistle or
from Dograh's own login, does not see it in any list, count or report, and
gets a 404 for its id, as though it did not exist.

Fails closed: a token without the claim (Dograh's own login, or one minted by
a Hopwhistle that predates the claim) is not the owner.

Only user-facing requests are narrowed. The flag lives in a context variable
that starts unrestricted, and only the email/password auth path narrows it.
So API-key callers (Hopwhistle's server, which starts calls on these agents),
telephony webhooks, the call runtime and background workers keep reaching
every agent, which is what keeps an owner-only agent taking calls.
"""

from contextvars import ContextVar

from sqlalchemy import and_, true

_owner_only_hidden: ContextVar[bool] = ContextVar("owner_only_hidden", default=False)


def token_is_platform_owner(payload: dict) -> bool:
    """True only when the token explicitly says so."""
    return payload.get("platform_owner") is True


def set_viewer_from_token(payload: dict) -> None:
    """Narrow this request unless its token belongs to the platform owner."""
    _owner_only_hidden.set(not token_is_platform_owner(payload))


def owner_only_hidden() -> bool:
    """Whether this request must not see owner-only agents."""
    return _owner_only_hidden.get()


def visible_workflows():
    """SQL condition on WorkflowModel: the agents this request may see."""
    from api.db.models import WorkflowModel

    if owner_only_hidden():
        return WorkflowModel.owner_only.is_(False)
    return true()


def workflow_in_org(organization_id):
    """``WorkflowModel.organization_id == organization_id``, less what this
    request may not see. Use it wherever a request scopes workflows (or the
    runs joined to them) to the caller's organization."""
    from api.db.models import WorkflowModel

    return and_(WorkflowModel.organization_id == organization_id, visible_workflows())
