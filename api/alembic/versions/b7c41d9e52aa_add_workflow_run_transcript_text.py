"""add workflow_run transcript_text with a full-text index

The transcript already exists inside ``workflow_runs.logs``, but only as a JSON
event array, which cannot be searched. This adds a plain-text copy written when
the call ends, plus a GIN index over its ``to_tsvector``, so "every call where
the caller said 'too expensive'" is a query rather than a re-parse of every run.

Revision ID: b7c41d9e52aa
Revises: 00b0201ad918
Create Date: 2026-09-20

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c41d9e52aa"
down_revision = "00b0201ad918"
branch_labels = None
depends_on = None

# 'english' rather than 'simple': stemming makes "pricing" match "priced", which
# is what someone mining objections actually wants.
_INDEX_NAME = "ix_workflow_runs_transcript_text_fts"


def upgrade() -> None:
    # Nullable with no default, so this is a catalog-only change: no table
    # rewrite, no long lock.
    op.add_column(
        "workflow_runs",
        sa.Column("transcript_text", sa.Text(), nullable=True),
    )
    # The index build is the dangerous half. A plain CREATE INDEX holds a SHARE
    # lock on workflow_runs until it finishes, which blocks every in-flight
    # call's run update — on a table with real call history that is a
    # calls-dropping window. CONCURRENTLY trades a slower build for not
    # blocking writes, and cannot run inside a transaction, so step outside the
    # per-migration transaction env.py opens.
    #
    # If a concurrent build is interrupted it leaves an INVALID index behind;
    # re-running this migration is safe (IF NOT EXISTS), but the invalid index
    # must be dropped first or it will simply be skipped:
    #   DROP INDEX CONCURRENTLY ix_workflow_runs_transcript_text_fts;
    with op.get_context().autocommit_block():
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME}
            ON workflow_runs
            USING GIN (to_tsvector('english', coalesce(transcript_text, '')))
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.drop_column("workflow_runs", "transcript_text")
