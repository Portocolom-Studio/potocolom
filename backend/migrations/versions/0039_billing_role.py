"""A billing role bound to one named account

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_WITH_BILLING = "('viewer', 'user', 'admin', 'billing')"
_WITHOUT_BILLING = "('viewer', 'user', 'admin')"


def upgrade() -> None:
    # NOT VALID first and VALIDATE after its own commit: a check added in one
    # step scans the table under ACCESS EXCLUSIVE, and old API tasks still
    # serving during an upgrade would wait on every read of users. VALIDATE
    # takes only SHARE UPDATE EXCLUSIVE.
    for table, name in (("users", "users_role"), ("invitations", "invitations_role")):
        op.drop_constraint(name, table, type_="check")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                   f"CHECK (role IN {_WITH_BILLING}) NOT VALID")
    op.create_table(
        "billing_bindings",
        sa.Column("delegate_user_id", sa.Uuid(), nullable=False),
        sa.Column("subject_user_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["delegate_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("delegate_user_id"),
        sa.UniqueConstraint("subject_user_id", name="billing_bindings_one_subject"),
        sa.CheckConstraint("delegate_user_id <> subject_user_id",
                           name="billing_bindings_distinct"),
    )
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE users VALIDATE CONSTRAINT users_role")
        op.execute("ALTER TABLE invitations VALIDATE CONSTRAINT invitations_role")


def downgrade() -> None:
    connection = op.get_bind()
    billing = connection.execute(sa.text(
        "SELECT (SELECT count(*) FROM users WHERE role = 'billing') "
        "+ (SELECT count(*) FROM invitations WHERE role = 'billing')"
    )).scalar_one()
    if billing:
        raise RuntimeError(
            "cannot drop the billing role while a user or an invitation still holds it")
    op.drop_table("billing_bindings")
    op.drop_constraint("users_role", "users", type_="check")
    op.create_check_constraint("users_role", "users", f"role IN {_WITHOUT_BILLING}")
    op.drop_constraint("invitations_role", "invitations", type_="check")
    op.create_check_constraint(
        "invitations_role", "invitations", f"role IN {_WITHOUT_BILLING}")
