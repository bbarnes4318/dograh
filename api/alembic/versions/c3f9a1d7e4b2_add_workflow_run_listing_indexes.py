"""add indexes for the workflow-run list pages

Every run list (Agent Runs, a workflow's runs, a campaign's runs) is "newest
first, 50 at a time", but ``workflow_runs`` had no index on ``created_at`` and
``workflows`` had none on ``organization_id``. So each page load joined and
sorted every run the org (or workflow, or campaign) ever made just to return
the top 50 — which grows linearly with call history.

Revision ID: c3f9a1d7e4b2
Revises: a4c8e2f61b93
Create Date: 2026-10-05

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3f9a1d7e4b2"
down_revision = "a4c8e2f61b93"
branch_labels = None
depends_on = None

_INDEXES = [
    (
        "idx_workflow_runs_workflow_id_created_at",
        "workflow_runs (workflow_id, created_at DESC NULLS LAST)",
    ),
    (
        "idx_workflow_runs_campaign_id_created_at",
        "workflow_runs (campaign_id, created_at DESC NULLS LAST)",
    ),
    ("idx_workflow_runs_created_at", "workflow_runs (created_at DESC NULLS LAST)"),
    ("ix_workflows_organization_id", "workflows (organization_id)"),
]


def upgrade() -> None:
    # CONCURRENTLY so the build does not block in-flight calls' run updates
    # (see b7c41d9e52aa for the full reasoning). If a build is interrupted it
    # leaves an INVALID index that IF NOT EXISTS will skip; drop it first.
    with op.get_context().autocommit_block():
        for name, target in _INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
