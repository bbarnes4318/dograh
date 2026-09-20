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
    op.add_column(
        "workflow_runs",
        sa.Column("transcript_text", sa.Text(), nullable=True),
    )
    op.execute(
        f"""
        CREATE INDEX {_INDEX_NAME}
        ON workflow_runs
        USING GIN (to_tsvector('english', coalesce(transcript_text, '')))
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
    op.drop_column("workflow_runs", "transcript_text")
