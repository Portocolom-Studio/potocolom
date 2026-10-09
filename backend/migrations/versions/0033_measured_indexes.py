"""index audit search, the admin asset count and the purge sweep

Revision ID: 0033
Revises: 0032

Measured on a 50k user, 300k asset, 200k audit row fixture: audit search by
target 8.4 ms to 0.34 ms, by a rare action 4.3 ms to 1.1 ms, the admin asset
count 9.7 ms to 0.04 ms, and the purge sweep 569 buffers to 171, with a generic
plan too. A search by target and action together reads both audit indexes as a
bitmap AND, so a three-column index would buy nothing. The purge index is not
partial on the purge states: the sweep binds them as parameters, and a generic
plan cannot use a partial index whose predicate it cannot prove.
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Drop before every create: a concurrent build that failed leaves an
        # INVALID index behind, and CREATE INDEX CONCURRENTLY refuses to
        # replace a relation that already exists.
        op.drop_index(
            "audit_events_target",
            table_name="audit_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "audit_events_target",
            "audit_events",
            ["target_user_id", "occurred_at"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "audit_events_action",
            table_name="audit_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "audit_events_action",
            "audit_events",
            ["action", "occurred_at"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "assets_user",
            table_name="assets",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "assets_user",
            "assets",
            ["user_id"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "users_purge_due",
            table_name="users",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "users_purge_due",
            "users",
            ["state", "deletion_requested_at"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "users_purge_due",
            table_name="users",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "assets_user",
            table_name="assets",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "audit_events_action",
            table_name="audit_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "audit_events_target",
            table_name="audit_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
