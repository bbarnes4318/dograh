"""add workflows.owner_only: agents only the platform owner sees

Every Hopwhistle user who opens AI Voice is signed in as the same shared Dograh
user, so every agent in that workspace was visible to all of them. An agent
marked ``owner_only`` is left out of every list, count, report and lookup for
a viewer whose token does not say they are the platform owner
(api/services/auth/viewer.py).

Purely additive: a NOT NULL column with a server default, so the code still
running during a switch keeps inserting rows. Which agents are owner-only is
data, set with an UPDATE after the deploy, not here.

Revision ID: e4b7a2c9d013
Revises: d8e2f5a9c164
Create Date: 2026-11-08

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e4b7a2c9d013"
down_revision = "d8e2f5a9c164"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column(
            "owner_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workflows", "owner_only")
