"""add inbound routing groups

A routing group splits inbound calls on a set of phone numbers across several
workflows by weight (e.g. 50% agent 15 / 50% agent 25). Phone numbers join a
group through the new nullable ``telephony_phone_numbers.inbound_routing_group_id``.
``inbound_workflow_id`` is untouched and stays the fallback, so this migration
changes no existing number's routing: every row starts with no group.

Revision ID: 7b3e9c41d2a6
Revises: c3f9a1d7e4b2
Create Date: 2026-10-28

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "7b3e9c41d2a6"
down_revision = "c3f9a1d7e4b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_routing_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "sticky_by_caller",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inbound_routing_groups_id", "inbound_routing_groups", ["id"])
    op.create_index(
        "ix_inbound_routing_groups_org", "inbound_routing_groups", ["organization_id"]
    )

    op.create_table(
        "inbound_routing_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 100", name="ck_inbound_routing_targets_weight"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["inbound_routing_groups.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "workflow_id", name="uq_inbound_routing_targets_group_workflow"
        ),
    )
    op.create_index("ix_inbound_routing_targets_id", "inbound_routing_targets", ["id"])

    op.create_table(
        "inbound_routing_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "details", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["inbound_routing_groups.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inbound_routing_audit_id", "inbound_routing_audit", ["id"])
    op.create_index(
        "ix_inbound_routing_audit_org_created",
        "inbound_routing_audit",
        ["organization_id", "created_at"],
    )

    # Nullable with no default: adding it is a metadata-only change and every
    # existing number keeps routing to its inbound_workflow_id.
    op.add_column(
        "telephony_phone_numbers",
        sa.Column(
            "inbound_routing_group_id",
            sa.Integer(),
            sa.ForeignKey(
                "inbound_routing_groups.id",
                name="fk_phone_numbers_inbound_routing_group",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_phone_numbers_routing_group",
        "telephony_phone_numbers",
        ["inbound_routing_group_id"],
        postgresql_where=sa.text("inbound_routing_group_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_phone_numbers_routing_group", table_name="telephony_phone_numbers"
    )
    op.drop_constraint(
        "fk_phone_numbers_inbound_routing_group",
        "telephony_phone_numbers",
        type_="foreignkey",
    )
    op.drop_column("telephony_phone_numbers", "inbound_routing_group_id")
    op.drop_index(
        "ix_inbound_routing_audit_org_created", table_name="inbound_routing_audit"
    )
    op.drop_index("ix_inbound_routing_audit_id", table_name="inbound_routing_audit")
    op.drop_table("inbound_routing_audit")
    op.drop_index("ix_inbound_routing_targets_id", table_name="inbound_routing_targets")
    op.drop_table("inbound_routing_targets")
    op.drop_index("ix_inbound_routing_groups_org", table_name="inbound_routing_groups")
    op.drop_index("ix_inbound_routing_groups_id", table_name="inbound_routing_groups")
    op.drop_table("inbound_routing_groups")
