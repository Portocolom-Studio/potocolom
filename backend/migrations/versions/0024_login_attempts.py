"""how many sign-ins one identifier, or one caller, has started lately

Revision ID: 0024
Revises: 0023

A password login mints a challenge, and a challenge carries a budget of ten
guesses that runs out. Nothing bounded how often a challenge could be started,
so ten guesses every ten minutes was available for as long as an attacker cared
to keep asking, and the budget bounded nothing (#423). This table is the bound.

It counts in PostgreSQL rather than in the process, because a per-process
counter is right only while there is one process, and the cloud profile runs
more than one: two replicas would each admit the whole allowance and the limit
would quietly become twice what it says.

Both subjects are hashed. The identifier is whatever address the caller typed,
so the plain column would collect addresses belonging to people with no account
here, and the caller's address is raw IP, which the specification keeps to
expiring keys only. Rows carry the window's end and are pruned once it passes.

An address row also carries the moment its next attempt may be answered. The
wait against an address is the whole bound there, and it is only a bound when
the turns are taken one at a time: attempts that overlap otherwise serve the
same eight seconds together and a flood pays for one of them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("subject", sa.LargeBinary(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope", "subject"),
        sa.CheckConstraint("scope IN ('identifier', 'address')", name="login_attempts_scope"),
    )


def downgrade() -> None:
    op.drop_table("login_attempts")
