"""add an index for "has this number called an AI agent before?"

Every inbound call now checks whether its caller has already had an inbound
call answered (services/telephony/repeat_callers.py). The check matches on the
last ten digits of ``initial_context->>'caller_number'``; without this index it
would scan every inbound run the org ever took, on every call.

Revision ID: d8e2f5a9c164
Revises: 7b3e9c41d2a6
Create Date: 2026-10-30

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d8e2f5a9c164"
down_revision = "7b3e9c41d2a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CONCURRENTLY so the build does not block in-flight calls' run updates
    # (see b7c41d9e52aa). If a build is interrupted it leaves an INVALID index
    # that IF NOT EXISTS will skip; drop it first.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_workflow_runs_inbound_caller_digits ON workflow_runs "
            "(right(regexp_replace(initial_context->>'caller_number', "
            "'[^0-9]', '', 'g'), 10)) WHERE call_type = 'inbound'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_workflow_runs_inbound_caller_digits"
        )
