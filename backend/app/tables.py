"""ORM tables, the M2 subset of the data model in docs/architecture.md.

PostgreSQL is always the source of truth; in-memory queues and registries are
rebuilt from these rows (docs/blueprint.md, Redis layout).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, SmallInteger, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}  # timestamptz everywhere


ROLES = ("viewer", "user", "admin")
ACCOUNT_STATES = ("active", "suspended", "disabled", "deletion_pending", "purging")
IDENTITY_PROVIDERS = ("password", "google", "github")
AUTH_TOKEN_PURPOSES = ("setup", "reset", "recovery", "challenge")
OUTBOX_STATES = ("pending", "sent", "failed")
AUDIT_SEVERITIES = ("info", "high")
NORMALIZED_EMAIL = text("lower(btrim(email))")


def _one_of(column: str, allowed: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in allowed) + ")"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_one_of("role", ROLES), name="users_role"),
        CheckConstraint(_one_of("state", ACCOUNT_STATES), name="users_state"),
        # Two addresses that differ only by case or padding are one account.
        Index("users_email_normalized", NORMALIZED_EMAIL, unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True)
    mail_verified: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    role: Mapped[str] = mapped_column(Text, default="user")
    state: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))
    deleted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AuthIdentity(Base):
    """One way to prove an account: a local password, or a provider subject.

    A provider identity is the provider's proof and never carries a local
    credential, so the two cannot be confused for one another.
    """

    __tablename__ = "auth_identities"
    __table_args__ = (
        CheckConstraint(_one_of("provider", IDENTITY_PROVIDERS), name="auth_identities_provider"),
        CheckConstraint(
            "(provider = 'password') = (password_hash IS NOT NULL)",
            name="auth_identities_password_hash",
        ),
        UniqueConstraint("provider", "subject", name="auth_identities_provider_subject"),
        Index("auth_identities_one_password", "user_id",
              unique=True, postgresql_where=text("provider = 'password'")),
        # Accounts are one per normalized address, so their password identities
        # are too: a byte-exact subject would let two rows answer one login.
        Index("auth_identities_password_subject", text("lower(btrim(subject))"),
              unique=True, postgresql_where=text("provider = 'password'")),
        Index("auth_identities_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Session(Base):
    """Only the SHA-256 of the 32 random bytes is kept, never the token itself.

    No raw address and no full user agent: a durable record of who signed in
    from where is a standing disclosure risk that login does not need.
    """

    __tablename__ = "sessions"
    __table_args__ = (Index("sessions_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    remember_me: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    absolute_expires_at: Mapped[datetime]
    idle_expires_at: Mapped[datetime | None]
    recent_auth_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AuthToken(Base):
    """A one-use capability, not a principal: setup, reset, recovery, or the
    pre-session challenge a primary login mints before TOTP is checked.

    Setup carries no user because nobody has claimed the installation yet.
    """

    __tablename__ = "auth_tokens"
    __table_args__ = (
        CheckConstraint(_one_of("purpose", AUTH_TOKEN_PURPOSES), name="auth_tokens_purpose"),
        CheckConstraint("(purpose = 'setup') = (user_id IS NULL)",
                        name="auth_tokens_setup_has_no_user"),
        Index("auth_tokens_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(_one_of("role", ROLES), name="invitations_role"),
        # One open invitation per address; accepted and revoked ones stay as
        # the record of what happened.
        Index("invitations_one_open", NORMALIZED_EMAIL, unique=True,
              postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AuthFactor(Base):
    """A TOTP secret, encrypted under a root key ring purpose key.

    key_version is what makes rotation a bounded indexed sweep rather than a
    scan that parses every blob to find the ones still on the old key.
    """

    __tablename__ = "auth_factors"
    __table_args__ = (
        CheckConstraint("kind = 'totp'", name="auth_factors_kind"),
        UniqueConstraint("user_id", "kind", name="auth_factors_one_per_kind"),
        Index("auth_factors_key_version", "key_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(SmallInteger)
    # The last time step this factor accepted. A code is good once.
    last_step: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"
    __table_args__ = (
        UniqueConstraint("user_id", "code_hash", name="recovery_codes_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[bytes] = mapped_column(LargeBinary)
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class MailOutbox(Base):
    """Every email capability is durable here before anyone tries to deliver it,
    so a delivery outage queues and retries instead of losing the capability."""

    __tablename__ = "mail_outbox"
    __table_args__ = (
        CheckConstraint(_one_of("state", OUTBOX_STATES), name="mail_outbox_state"),
        Index("mail_outbox_due", "state", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    to_email: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, default="pending", server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Model(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    capabilities: Mapped[list] = mapped_column(JSONB)
    parameters_schema: Mapped[dict] = mapped_column(JSONB)
    min_vram_gb: Mapped[int]


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"))
    params: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, default="queued")  # running, succeeded, failed
    attempt: Mapped[int] = mapped_column(default=1)  # retry once, then fail (docs/decisions.md)
    gpu_ms: Mapped[int | None]
    input_fetch_ms: Mapped[int | None]
    load_ms: Mapped[int | None]
    postprocess_ms: Mapped[int | None]
    failure_reason: Mapped[str | None] = mapped_column(Text)
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"))
    dispatched_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    starred_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    parent_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"))
    storage_key: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    width: Mapped[int]
    height: Mapped[int]
    share_token: Mapped[str | None] = mapped_column(Text)  # null unless shared
    expires_at: Mapped[datetime | None]  # set for trial accounts in the cloud


class GpuSample(Base):
    __tablename__ = "gpu_samples"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[str] = mapped_column(Text)
    sampled_at: Mapped[datetime]
    util_pct: Mapped[int | None] = mapped_column(SmallInteger)
    vram_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    vram_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    temperature_c: Mapped[float | None] = mapped_column(Float)
    power_w: Mapped[float | None] = mapped_column(Float)
    loaded_models: Mapped[list | None] = mapped_column(JSONB)


class GpuSampleRollup(Base):
    __tablename__ = "gpu_sample_rollups"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(primary_key=True)
    sample_count: Mapped[int] = mapped_column(Integer)
    util_mean: Mapped[float | None] = mapped_column(Float)
    util_min: Mapped[int | None] = mapped_column(SmallInteger)
    util_max: Mapped[int | None] = mapped_column(SmallInteger)
    vram_used_pct_mean: Mapped[float | None] = mapped_column(Float)
    vram_used_pct_min: Mapped[int | None] = mapped_column(SmallInteger)
    vram_used_pct_max: Mapped[int | None] = mapped_column(SmallInteger)
    temperature_mean: Mapped[float | None] = mapped_column(Float)
    power_mean: Mapped[float | None] = mapped_column(Float)


class BenchmarkSession(Base):
    __tablename__ = "benchmark_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Who ran the suite, kept as provenance only; the history belongs to the install.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime]
    target_vram_gb: Mapped[float | None] = mapped_column(Float)
    prompt_count: Mapped[int]
    models: Mapped[list] = mapped_column(JSONB)
    variants_per_prompt: Mapped[int]
    total_jobs: Mapped[int]
    succeeded: Mapped[int]
    failed: Mapped[int]


class BenchmarkMeasurement(Base):
    __tablename__ = "benchmark_measurements"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_sessions.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    prompt_id: Mapped[int]
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(Text)
    cell_key: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB)
    model_load_ms: Mapped[int | None]
    state: Mapped[str] = mapped_column(Text)
    gpu_ms: Mapped[int | None]
    wall_s: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None]
    height: Mapped[int | None]
    job_id: Mapped[str | None] = mapped_column(Text)
    file: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    category_score: Mapped[float | None] = mapped_column(Float)
    gpu_ms: Mapped[int | None]
    duration_ms: Mapped[int | None]
    frames: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class UsageEventRollup(Base):
    __tablename__ = "usage_event_rollups"
    __table_args__ = (
        Index(
            "usage_event_rollups_key",
            "user_id",
            "bucket_date",
            "kind",
            "action",
            "model_id",
            text("COALESCE(tier, '')"),
            "category",
            unique=True,
        ),
        Index("usage_event_rollups_bucket_date", "bucket_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    bucket_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    event_count: Mapped[int] = mapped_column(BigInteger)
    category_score_sum: Mapped[float | None] = mapped_column(Float)
    category_score_count: Mapped[int] = mapped_column(BigInteger)
    gpu_ms_sum: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms_sum: Mapped[int | None] = mapped_column(BigInteger)
    frames_sum: Mapped[int | None] = mapped_column(BigInteger)


class TelemetryState(Base):
    __tablename__ = "telemetry_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    install_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    last_report_day: Mapped[date | None] = mapped_column(Date)


class PendingDelete(Base):
    """One row per blob the terminal paths could not remove (issue #254).

    The terminal paths swallow per-key delete failures so one bad key does not
    stop the rest, and this is the list of keys they tried and failed. The
    sweep owns attempts; a failure elsewhere refreshes last_error and
    reschedules. A row leaves only when its object is gone, so a row here is
    always either due or waiting out its backoff.
    """

    __tablename__ = "pending_deletes"

    storage_key: Mapped[str] = mapped_column(Text, primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    first_failed_at: Mapped[datetime]
    next_attempt_at: Mapped[datetime]

    # Named to match migration 0013: mapped_column(index=True) would call it
    # ix_pending_deletes_next_attempt_at, so a create_all schema and a migrated
    # one would differ and the next autogenerate would emit a spurious pair.
    __table_args__ = (Index("pending_deletes_due", "next_attempt_at"),)


class InstallationAuthState(Base):
    __tablename__ = "installation_auth_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    auth_mode: Mapped[str] = mapped_column(Text)
    root_key_version: Mapped[int | None] = mapped_column(SmallInteger)
    enabled_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class OAuthFlow(Base):
    """One redirect to a provider and back, and nothing else.

    The verifier and the nonce never leave this row, so a callback cannot be
    replayed or crafted: it has to match a flow this server started. Rows are
    one-use and short lived.
    """

    __tablename__ = "oauth_flows"
    # Named here as well as in the migration, so create_all and a migrated
    # schema agree and the next autogenerate does not propose dropping it.
    __table_args__ = (Index("oauth_flows_expires", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    provider: Mapped[str] = mapped_column(Text)
    verifier: Mapped[str] = mapped_column(Text)
    nonce: Mapped[str] = mapped_column(Text)
    # Set when the flow was started from a live session to link a provider to
    # it. Null means a sign-in attempt, which can only ever match an identity
    # somebody already linked.
    link_user_id: Mapped[uuid.UUID | None]
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class SuppressedAddress(Base):
    """An address the relay refused outright, or the provider reported back.

    Keyed by the normalized address, so recording the same bounce twice is the
    same row. Retrying a permanently undeliverable address only teaches the
    relay to distrust this sender.
    """

    __tablename__ = "suppressed_addresses"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AuditEvent(Base):
    """What a privileged action did, kept when the account that did it is gone.

    The actor and target are plain ids with no foreign key. A record is a
    historical fact, not a live relation: CASCADE would let an administrator
    erase what they did by deleting their own account, and SET NULL would let
    them erase who did it, which is the part an audit exists for.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(_one_of("severity", AUDIT_SEVERITIES), name="audit_events_severity"),
        Index("audit_events_occurred", "occurred_at"),
        Index("audit_events_actor", "actor_user_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime]
    actor_user_id: Mapped[uuid.UUID | None]
    actor_role: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_user_id: Mapped[uuid.UUID | None]
    object_ids: Mapped[list] = mapped_column(JSONB, default=list)
    object_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    truncated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    severity: Mapped[str] = mapped_column(Text, default="info", server_default=text("'info'"))


class WorkerIdentity(Base):
    __tablename__ = "workers"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device: Mapped[str | None] = mapped_column(Text)
    memory_mode: Mapped[str | None] = mapped_column(Text)
    last_seen: Mapped[datetime]
