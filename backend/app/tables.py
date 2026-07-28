"""ORM tables, the M2 subset of the data model in docs/architecture.md.

PostgreSQL is always the source of truth; in-memory queues and registries are
rebuilt from these rows (docs/blueprint.md, Redis layout).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Date, DateTime, Float, ForeignKey, Integer, SmallInteger, Text, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}  # timestamptz everywhere


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True)
    role: Mapped[str] = mapped_column(Text, default="user")
    deleted_at: Mapped[datetime | None]
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


class TelemetryState(Base):
    __tablename__ = "telemetry_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    install_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    last_report_day: Mapped[date | None] = mapped_column(Date)


class WorkerIdentity(Base):
    __tablename__ = "workers"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device: Mapped[str | None] = mapped_column(Text)
    memory_mode: Mapped[str | None] = mapped_column(Text)
    last_seen: Mapped[datetime]
