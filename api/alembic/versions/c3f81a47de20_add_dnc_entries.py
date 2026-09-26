"""add dnc_entries and a suppressed state for queued runs

Nothing consulted a suppression list before dialling: every uploaded row was
called as given, and ``DNC`` existed only as a disposition written after the
call. This adds the list, and a ``suppressed`` queued-run state so a lead
skipped for being on it is distinguishable from one that failed.

Revision ID: c3f81a47de20
Revises: b7c41d9e52aa
Create Date: 2026-09-22

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3f81a47de20"
down_revision = "b7c41d9e52aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres forbids using a new enum value in the transaction that added it.
    # Nothing here writes the value, so adding it alongside the table is safe.
    op.execute("ALTER TYPE queued_run_state ADD VALUE IF NOT EXISTS 'suppressed'")

    op.create_table(
        "dnc_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(length=20), nullable=False),
        sa.Column("raw_input", sa.String(length=64), nullable=True),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="manual"
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workflow_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "phone_number", name="uq_dnc_entries_org_number"
        ),
    )
    op.create_index(op.f("ix_dnc_entries_id"), "dnc_entries", ["id"])
    op.create_index(
        "idx_dnc_entries_org_number", "dnc_entries", ["organization_id", "phone_number"]
    )
    op.create_index(
        "idx_dnc_entries_org_created", "dnc_entries", ["organization_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_dnc_entries_org_created", table_name="dnc_entries")
    op.drop_index("idx_dnc_entries_org_number", table_name="dnc_entries")
    op.drop_index(op.f("ix_dnc_entries_id"), table_name="dnc_entries")
    op.drop_table("dnc_entries")
    # The enum value is left in place: Postgres cannot drop one, and rebuilding
    # the type would require rewriting every queued_runs row.
