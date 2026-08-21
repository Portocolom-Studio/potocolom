"""account schema and the installation root key version

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

ROLES = "('viewer', 'user', 'admin')"
ACCOUNT_STATES = "('active', 'suspended', 'disabled', 'deletion_pending', 'purging')"
IDENTITY_PROVIDERS = "('password', 'google', 'github')"
AUTH_TOKEN_PURPOSES = "('setup', 'reset', 'recovery', 'challenge')"
OUTBOX_STATES = "('pending', 'sent', 'failed')"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def _user_id(nullable: bool = False) -> sa.Column:
    return sa.Column("user_id", sa.Uuid(),
                     sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=nullable)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.text("now()"))


def upgrade() -> None:
    # An install upgrading from AUTH_MODE=none holds one row, the implicit local
    # user. Two rows whose emails differ only by case would fail this index,
    # which is the correct outcome: the accounts release treats them as one
    # account and nothing here may guess which one wins.
    op.execute("CREATE UNIQUE INDEX users_email_normalized ON users (lower(btrim(email)))")
    op.add_column("users", sa.Column("mail_verified", sa.Boolean(), nullable=False,
                                     server_default=sa.text("false")))
    op.add_column("users", sa.Column("state", sa.Text(), nullable=False,
                                     server_default=sa.text("'active'")))
    op.create_check_constraint("users_role", "users", f"role IN {ROLES}")
    op.create_check_constraint("users_state", "users", f"state IN {ACCOUNT_STATES}")

    op.add_column("installation_auth_state",
                  sa.Column("root_key_version", sa.SmallInteger(), nullable=True))

    op.create_table(
        "auth_identities",
        _uuid_pk(),
        _user_id(),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(f"provider IN {IDENTITY_PROVIDERS}", name="auth_identities_provider"),
        sa.CheckConstraint("(provider = 'password') = (password_hash IS NOT NULL)",
                           name="auth_identities_password_hash"),
        sa.UniqueConstraint("provider", "subject", name="auth_identities_provider_subject"),
    )
    op.create_index("auth_identities_one_password", "auth_identities", ["user_id"], unique=True,
                    postgresql_where=sa.text("provider = 'password'"))
    op.create_index("auth_identities_user", "auth_identities", ["user_id"])

    op.create_table(
        "sessions",
        _uuid_pk(),
        _user_id(),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("remember_me", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recent_auth_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    # Revoking every session an account holds is a single indexed sweep, which
    # is what a role change, a credential change or a suspension each trigger.
    op.create_index("sessions_user", "sessions", ["user_id"])

    op.create_table(
        "auth_tokens",
        _uuid_pk(),
        _user_id(nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(f"purpose IN {AUTH_TOKEN_PURPOSES}", name="auth_tokens_purpose"),
    )
    op.create_index("auth_tokens_user", "auth_tokens", ["user_id"])

    op.create_table(
        "invitations",
        _uuid_pk(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("invited_by", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("accepted_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(f"role IN {ROLES}", name="invitations_role"),
    )
    # One open invitation per address, while an accepted or revoked one stays
    # as the record of what happened.
    op.execute("CREATE UNIQUE INDEX invitations_one_open ON invitations (lower(btrim(email))) "
               "WHERE accepted_at IS NULL AND revoked_at IS NULL")

    op.create_table(
        "auth_factors",
        _uuid_pk(),
        _user_id(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.SmallInteger(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint("kind = 'totp'", name="auth_factors_kind"),
        sa.UniqueConstraint("user_id", "kind", name="auth_factors_one_per_kind"),
    )
    # Rotation re-encrypts by key version, so the sweep reads only the rows
    # still on the version being removed.
    op.create_index("auth_factors_key_version", "auth_factors", ["key_version"])

    op.create_table(
        "recovery_codes",
        _uuid_pk(),
        _user_id(),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint("user_id", "code_hash", name="recovery_codes_unique"),
    )

    op.create_table(
        "mail_outbox",
        _uuid_pk(),
        sa.Column("to_email", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(f"state IN {OUTBOX_STATES}", name="mail_outbox_state"),
    )
    op.create_index("mail_outbox_due", "mail_outbox", ["state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("mail_outbox_due", table_name="mail_outbox")
    op.drop_table("mail_outbox")
    op.drop_table("recovery_codes")
    op.drop_index("auth_factors_key_version", table_name="auth_factors")
    op.drop_table("auth_factors")
    op.drop_index("invitations_one_open", table_name="invitations")
    op.drop_table("invitations")
    op.drop_index("auth_tokens_user", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_index("sessions_user", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("auth_identities_user", table_name="auth_identities")
    op.drop_index("auth_identities_one_password", table_name="auth_identities")
    op.drop_table("auth_identities")
    op.drop_column("installation_auth_state", "root_key_version")
    op.drop_constraint("users_state", "users", type_="check")
    op.drop_constraint("users_role", "users", type_="check")
    op.drop_column("users", "state")
    op.drop_column("users", "mail_verified")
    op.drop_index("users_email_normalized", table_name="users")
