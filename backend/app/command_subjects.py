"""Canonical subject and browser-binding policy for protocol 6 commands."""

from __future__ import annotations

import uuid
import hashlib
import hmac
import json
from collections.abc import Collection

from sqlalchemy import text

from app import db
from app.account_lock import hold_the_account
from app.settings import get_settings

MAX_SEQUENCE = 2**63 - 1
SUBJECT_FIELDS = {
    "open_session": ("session_id", "attempt_id", "control_generation"),
    "update_session": ("session_id", "attempt_id", "control_generation"),
    "close_session": ("session_id", "attempt_id", "control_generation"),
    "dispatch_job": ("job_id", "dispatch_sequence"),
    "cancel_job": ("job_id", "dispatch_sequence"),
}
SUBJECT_COMMANDS = {
    "job": ("dispatch_job", "cancel_job"),
    "session": ("open_session", "update_session", "close_session"),
}


class CommandRefused(RuntimeError):
    pass


def validate_command_identity(command_type, account_user_id, fields) -> None:
    if command_type not in SUBJECT_FIELDS:
        raise CommandRefused("command type is not supported")
    if not isinstance(account_user_id, uuid.UUID):
        raise CommandRefused("command account identity is unavailable")
    if not isinstance(fields, dict) or any(
        key in fields for key in {
            "type", "worker_id", "incarnation", "region", "owner_epoch", "lease_id",
            "lease_expires_at", "grant_nonce", "command_sequence", "command_id", "body_hash",
        }
    ):
        raise CommandRefused("command fields are invalid")


def command_size_limit(kind: str, parameter_limit: int, small_limit: int) -> int:
    return parameter_limit if kind in {"dispatch_job", "open_session", "update_session"} else small_limit


async def current_owner(session, account_user_id: uuid.UUID) -> bool:
    principal = (await session.execute(
        text("SELECT role, state FROM users WHERE id = :user_id FOR SHARE"),
        {"user_id": account_user_id},
    )).mappings().one_or_none()
    return principal is not None and principal["role"] in {"user", "admin"} and principal[
        "state"
    ] == "active"


async def lock_browser_binding(
    session,
    account_user_id: uuid.UUID,
    auth_session_id: uuid.UUID | None,
) -> tuple[object, object] | None:
    if not await current_owner(session, account_user_id):
        return None
    auth_mode = get_settings().auth_mode
    if auth_mode == "none":
        return (None, None) if db.local_user_id == account_user_id and auth_session_id is None else None
    if auth_mode != "accounts" or auth_session_id is None:
        return None
    bound = (await session.execute(
        text(
            "SELECT idle_expires_at, absolute_expires_at FROM sessions "
            "WHERE id = :session_id AND user_id = :user_id AND revoked_at IS NULL FOR SHARE"
        ),
        {"session_id": auth_session_id, "user_id": account_user_id},
    )).mappings().one_or_none()
    return (bound["idle_expires_at"], bound["absolute_expires_at"]) if bound else None


async def binding_is_live(session, deadlines: tuple[object, object] | None) -> bool:
    if deadlines is None:
        return False
    idle, absolute = deadlines
    if absolute is None:
        return True
    return bool(await session.scalar(
        text(
            "SELECT :absolute_expires_at > clock_timestamp() AND "
            "(CAST(:idle_expires_at AS timestamptz) IS NULL OR "
            "CAST(:idle_expires_at AS timestamptz) > clock_timestamp())"
        ),
        {"absolute_expires_at": absolute, "idle_expires_at": idle},
    ))


async def current_principal(
    session, account_user_id: uuid.UUID, auth_session_id: uuid.UUID | None
) -> bool:
    return await binding_is_live(
        session, await lock_browser_binding(session, account_user_id, auth_session_id)
    )


async def prepare_command_subject(
    session, command_type, fields, *, account_user_id, session_context, job_context,
    worker_id, incarnation, expected_transport_owner,
):
    claim = None
    cancel_attempt = None
    message_fields = fields
    committed_desired = None
    if command_type == "open_session":
        if not isinstance(account_user_id, uuid.UUID) or not isinstance(session_context, dict):
            raise CommandRefused("session claim identity is unavailable")
        session_id = uuid.UUID(fields["session_id"])
        existing = (await session.execute(
            text(
                "SELECT user_id, browser_owner_id, control_generation FROM realtime_sessions "
                "WHERE id = :session_id FOR UPDATE"
            ),
            {"session_id": session_id},
        )).mappings().one_or_none()
        if existing is not None and (
            existing["user_id"] != account_user_id
            or existing["browser_owner_id"] != session_context["browser_owner_id"]
            or existing["control_generation"] > fields["control_generation"]
        ):
            raise CommandRefused("session claim identity is stale")
        claim = {
            "kind": "session",
            "subject_id": session_id,
            "attempt_id": uuid.UUID(fields["attempt_id"]),
            "control_generation": fields["control_generation"],
            "range_id": uuid.UUID(fields["work_budget"]["range_id"]),
            "account_user_id": account_user_id,
            "session_context": session_context,
        }
    elif command_type == "update_session":
        if (not isinstance(account_user_id, uuid.UUID)
                or not isinstance(session_context, dict)):
            raise CommandRefused("session update identity is unavailable")
        session_id = uuid.UUID(fields["session_id"])
        attempt_id = uuid.UUID(fields["attempt_id"])
        generation = fields["control_generation"]
        base_revision = session_context.get("base_desired_revision")
        revision = session_context.get("desired_revision")
        if (not isinstance(base_revision, int) or isinstance(base_revision, bool)
                or base_revision < 1 or base_revision >= MAX_SEQUENCE
                or revision != base_revision + 1 or isinstance(revision, bool)
                or revision >= MAX_SEQUENCE
                or fields.get("params_revision") != revision
                or fields.get("model_id") != session_context.get("model_id")
                or fields.get("params") != session_context.get("params")):
            raise CommandRefused("session desired update changed before commit")
        entity = (await session.execute(
            text(
                "SELECT user_id, auth_session_id, browser_owner_id, model_id, "
                "desired_revision, control_generation "
                "FROM realtime_sessions "
                "WHERE id = :session_id AND control_generation = :generation FOR UPDATE"
            ),
            {"session_id": session_id, "generation": generation},
        )).mappings().one_or_none()
        attempt = (await session.execute(
            text(
                "SELECT attempt_id, worker_id, incarnation FROM realtime_session_attempts "
                "WHERE session_id = :session_id AND control_generation = :generation "
                "AND attempt_id = :attempt_id AND state IN ('opening', 'running') "
                "FOR UPDATE"
            ),
            {"session_id": session_id, "generation": generation,
             "attempt_id": attempt_id},
        )).mappings().one_or_none()
        if (entity is None or entity["user_id"] != account_user_id
                or entity["auth_session_id"] != session_context.get("auth_session_id")
                or entity["browser_owner_id"] != session_context.get("browser_owner_id")
                or not await current_principal(
                    session, account_user_id, entity["auth_session_id"]
                )
                or entity["model_id"] != session_context.get("model_id")
                or entity["desired_revision"] != base_revision
                or attempt is None or attempt["worker_id"] != worker_id
                or attempt["incarnation"] != incarnation
                or attempt["attempt_id"] != session_context.get("attempt_id")):
            raise CommandRefused("session update is stale")
        changed = await session.execute(
            text(
                "UPDATE realtime_sessions SET desired_params = CAST(:params AS jsonb), "
                "desired_revision = :revision WHERE id = :session_id "
                "AND desired_revision = :base_revision "
                "RETURNING desired_params, desired_revision"
            ),
            {
                "params": json.dumps(fields["params"]),
                "revision": revision,
                "base_revision": base_revision,
                "session_id": session_id,
            },
        )
        committed_desired = changed.mappings().one_or_none()
        if committed_desired is None:
            raise CommandRefused("session desired update lost its compare-and-set")
        message_fields = {**fields, "model_id": entity["model_id"]}
    elif command_type == "close_session":
        if not isinstance(account_user_id, uuid.UUID):
            raise CommandRefused("session close identity is unavailable")
        session_id = uuid.UUID(fields["session_id"])
        attempt_id = uuid.UUID(fields["attempt_id"])
        generation = fields["control_generation"]
        entity = (await session.execute(
            text(
                "SELECT user_id, state, auth_session_id, browser_owner_id "
                "FROM realtime_sessions WHERE id = :session_id "
                "AND control_generation = :generation FOR UPDATE"
            ),
            {"session_id": session_id, "generation": generation},
        )).mappings().one_or_none()
        attempt = (await session.execute(
            text(
                "SELECT worker_id, incarnation FROM realtime_session_attempts "
                "WHERE session_id = :session_id AND control_generation = :generation "
                "AND attempt_id = :attempt_id AND state IN ('opening', 'running') "
                "FOR UPDATE"
            ),
            {"session_id": session_id, "generation": generation,
             "attempt_id": attempt_id},
        )).mappings().one_or_none()
        if (entity is None or entity["user_id"] != account_user_id
                or entity["state"] != "live"
                or entity["browser_owner_id"] != expected_transport_owner
                or not await current_principal(
                    session, account_user_id, entity["auth_session_id"]
                )
                or attempt is None or attempt["worker_id"] != worker_id
                or attempt["incarnation"] != incarnation):
            raise CommandRefused("session close is stale")
        changed = await session.execute(
            text(
                "UPDATE realtime_sessions SET state = 'idle' WHERE id = :session_id "
                "AND control_generation = :generation AND state = 'live' RETURNING id"
            ),
            {"session_id": session_id, "generation": generation},
        )
        if changed.scalar_one_or_none() is None:
            raise CommandRefused("session release changed before commit")
    elif command_type == "dispatch_job":
        if not isinstance(account_user_id, uuid.UUID):
            raise CommandRefused("job claim account is unavailable")
        job_id = uuid.UUID(fields["job_id"])
        job = (await session.execute(
            text(
                "SELECT user_id, model_id, params, state, attempt, "
                "current_dispatch_sequence, source_asset_id FROM jobs "
                "WHERE id = :job_id FOR UPDATE"
            ),
            {"job_id": job_id},
        )).mappings().one_or_none()
        if (job is None or job["user_id"] != account_user_id
                or job["state"] != "queued" or job["model_id"] != fields["model_id"]
                or job["params"] != fields["params"]):
            raise CommandRefused("job claim is stale")
        if isinstance(job_context, dict) and (
            job_context.get("source_asset_id") != job["source_asset_id"]
        ):
            raise CommandRefused("job input changed before claim")
        if job["source_asset_id"] is not None:
            asset = await session.scalar(
                text(
                    "SELECT id FROM assets WHERE id = :asset_id AND user_id = :user_id "
                    "FOR UPDATE"
                ),
                {"asset_id": job["source_asset_id"], "user_id": account_user_id},
            )
            if asset is None:
                raise CommandRefused("job input is unavailable")
        dispatch_sequence = job["current_dispatch_sequence"] + 1
        if dispatch_sequence >= MAX_SEQUENCE:
            raise CommandRefused("job dispatch sequence is exhausted")
        message_fields = {**fields, "dispatch_sequence": dispatch_sequence}
        claim = {
            "kind": "job",
            "subject_id": job_id,
            "previous_dispatch_sequence": job["current_dispatch_sequence"],
            "dispatch_sequence": dispatch_sequence,
            "range_id": uuid.UUID(fields["work_budget"]["range_id"]),
            "dispatch_token": fields["dispatch_token"],
        }
    elif command_type == "cancel_job":
        if (not isinstance(account_user_id, uuid.UUID)
                or not isinstance(fields.get("dispatch_sequence"), int)
                or isinstance(fields.get("dispatch_sequence"), bool)
                or not isinstance(fields.get("dispatch_token"), str)
                or not fields["dispatch_token"].isascii()):
            raise CommandRefused("job cancellation identity is unavailable")
        job_id = uuid.UUID(fields["job_id"])
        job = (await session.execute(
            text(
                "SELECT user_id, state, current_dispatch_sequence FROM jobs "
                "WHERE id = :job_id FOR UPDATE"
            ),
            {"job_id": job_id},
        )).mappings().one_or_none()
        if (job is None or job["user_id"] != account_user_id
                or job["state"] != "cancelled"
                or job["current_dispatch_sequence"] != fields["dispatch_sequence"]):
            raise CommandRefused("job cancellation is stale")
        cancel_attempt = (await session.execute(
            text(
                "SELECT worker_id, incarnation, owner_epoch, dispatch_token_hash, state "
                "FROM job_attempts WHERE job_id = :job_id "
                "AND dispatch_sequence = :dispatch_sequence FOR UPDATE"
            ),
            {"job_id": job_id, "dispatch_sequence": fields["dispatch_sequence"]},
        )).mappings().one_or_none()
        token_hash = hashlib.sha256(fields["dispatch_token"].encode("ascii")).hexdigest()
        if (cancel_attempt is None
                or not hmac.compare_digest(cancel_attempt["dispatch_token_hash"], token_hash)):
            raise CommandRefused("job cancellation token is stale")

    return claim, cancel_attempt, message_fields, committed_desired


async def apply_command_claim(
    session, claim, *, worker_id, incarnation, account_user_id, command_id, worker
):
    if claim is not None:
        if await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM worker_physical_ranges "
                "WHERE range_id = :range_id)"
            ),
            {"range_id": claim["range_id"]},
        ):
            raise CommandRefused("physical range identity is already used")
        if claim["kind"] == "session":
            context = claim["session_context"]
            await session.execute(
                text(
                    "INSERT INTO realtime_sessions "
                    "(id, user_id, auth_session_id, browser_owner_id, region, model_id, "
                    "effective_params, desired_params, params_revision, desired_revision, "
                    "state, control_generation, owner_epoch, input_revision, created_at) "
                    "VALUES (:id, :user_id, :auth_session_id, :browser_owner_id, :region, "
                    ":model_id, CAST(:params AS jsonb), CAST(:params AS jsonb), 1, 1, "
                    "'assigning', :control_generation, :owner_epoch, 0, clock_timestamp()) "
                    "ON CONFLICT (id) DO UPDATE SET model_id = EXCLUDED.model_id, "
                    "effective_params = EXCLUDED.effective_params, "
                    "desired_params = EXCLUDED.desired_params, "
                    "desired_revision = EXCLUDED.desired_revision, state = 'assigning', "
                    "control_generation = EXCLUDED.control_generation, started_at = NULL, "
                    "ended_at = NULL, owner_epoch = EXCLUDED.owner_epoch"
                ),
                {
                    "id": claim["subject_id"],
                    "user_id": claim["account_user_id"],
                    "auth_session_id": context.get("auth_session_id"),
                    "browser_owner_id": context["browser_owner_id"],
                    "region": worker["region"],
                    "model_id": context["model_id"],
                    "params": json.dumps(context["params"]),
                    "control_generation": claim["control_generation"],
                    "owner_epoch": worker["owner_epoch"],
                },
            )


            await session.execute(
                text(
                    "INSERT INTO realtime_session_attempts "
                    "(session_id, control_generation, worker_id, incarnation, owner_epoch, "
                    "attempt_id, state, range_id, created_at) VALUES (:session_id, :generation, "
                    ":worker_id, :incarnation, :owner_epoch, :attempt_id, 'opening', "
                    ":range_id, clock_timestamp())"
                ),
                {
                    "session_id": claim["subject_id"],
                    "generation": claim["control_generation"],
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "owner_epoch": worker["owner_epoch"],
                    "attempt_id": claim["attempt_id"],
                    "range_id": claim["range_id"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO worker_physical_ranges "
                    "(range_id, worker_id, incarnation, region, owner_epoch, account_user_id, kind, subject_id, "
                    "attempt_id, control_generation, state) VALUES (:range_id, :worker_id, "
                    ":incarnation, :region, :owner_epoch, :account_user_id, 'session', :subject_id, :attempt_id, "
                    ":generation, 'outstanding')"
                ),
                {
                    "range_id": claim["range_id"],
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "region": worker["region"],
                    "owner_epoch": worker["owner_epoch"],
                    "account_user_id": account_user_id,
                    "subject_id": claim["subject_id"],
                    "attempt_id": claim["attempt_id"],
                    "generation": claim["control_generation"],
                },
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO job_attempts "
                    "(job_id, dispatch_sequence, command_id, worker_id, incarnation, owner_epoch, "
                    "state, dispatch_token_hash, range_id, created_at) VALUES (:job_id, "
                    ":dispatch_sequence, :command_id, :worker_id, :incarnation, :owner_epoch, "
                    "'dispatching', :token_hash, :range_id, clock_timestamp())"
                ),
                {
                    "job_id": claim["subject_id"],
                    "dispatch_sequence": claim["dispatch_sequence"],
                    "command_id": command_id,
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "owner_epoch": worker["owner_epoch"],
                    "token_hash": hashlib.sha256(claim["dispatch_token"].encode()).hexdigest(),
                    "range_id": claim["range_id"],
                },
            )
            updated = await session.execute(
                text(
                    "UPDATE jobs SET state = 'running', current_dispatch_sequence = :sequence, "
                    "dispatched_at = clock_timestamp() WHERE id = :job_id AND state = 'queued' "
                    "AND current_dispatch_sequence = :previous_sequence RETURNING id"
                ),
                {
                    "sequence": claim["dispatch_sequence"],
                    "job_id": claim["subject_id"],
                    "previous_sequence": claim["previous_dispatch_sequence"],
                },
            )
            if updated.scalar_one_or_none() is None:
                raise CommandRefused("job claim changed before commit")
            await session.execute(
                text(
                    "INSERT INTO worker_physical_ranges "
                    "(range_id, worker_id, incarnation, region, owner_epoch, account_user_id, kind, subject_id, "
                    "dispatch_sequence, state) VALUES (:range_id, :worker_id, :incarnation, "
                    ":region, :owner_epoch, :account_user_id, 'job', :job_id, :dispatch_sequence, 'outstanding')"
                ),
                {
                    "range_id": claim["range_id"],
                    "worker_id": worker_id,
                    "incarnation": incarnation,
                    "region": worker["region"],
                    "owner_epoch": worker["owner_epoch"],
                    "account_user_id": account_user_id,
                    "job_id": claim["subject_id"],
                    "dispatch_sequence": claim["dispatch_sequence"],
                },
            )




async def lock_delivery_subject(
    session, command, body, *, worker_id, incarnation, transport_owner_id
):
    kind = body["type"]
    binding_deadlines = None
    if kind.endswith("session"):
        entity = (await session.execute(
            text(
                "SELECT user_id, auth_session_id, browser_owner_id, model_id, state, "
                "control_generation FROM realtime_sessions "
                "WHERE id = :subject_id FOR UPDATE"
            ), {"subject_id": command.subject_id},
        )).mappings().one_or_none()
        if (entity is None or entity["user_id"] != command.account_user_id
                or entity["control_generation"] != command.subject_sequence):
            raise CommandRefused("session command is no longer current")
        binding_deadlines = await lock_browser_binding(
            session, command.account_user_id, entity["auth_session_id"]
        )
        if binding_deadlines is None:
            raise CommandRefused("session command principal is no longer current")
        expected_state = {
            "open_session": "assigning",
            "update_session": ("live", "idle"),
            "close_session": "idle",
        }.get(kind)
        if (expected_state is None or
                (entity["state"] not in expected_state if isinstance(expected_state, tuple)
                 else entity["state"] != expected_state)):
            raise CommandRefused("session command state is no longer current")
        if (entity["browser_owner_id"] != transport_owner_id
                or (kind in {"open_session", "update_session"}
                    and body.get("model_id") != entity["model_id"])
                ):
            raise CommandRefused("session command principal is no longer current")
        attempt = (await session.execute(
            text(
                "SELECT worker_id, incarnation, attempt_id, range_id, state FROM "
                "realtime_session_attempts WHERE session_id = :subject_id "
                "AND control_generation = :sequence AND attempt_id = :attempt_id "
                "AND state IN ('opening','running') "
                "FOR UPDATE"
            ), {"subject_id": command.subject_id,
                "sequence": command.subject_sequence,
                "attempt_id": uuid.UUID(body["attempt_id"])},
        )).mappings().one_or_none()
    else:
        entity = (await session.execute(
            text(
                "SELECT user_id, state, current_dispatch_sequence FROM jobs "
                "WHERE id = :subject_id FOR UPDATE"
            ), {"subject_id": command.subject_id},
        )).mappings().one_or_none()
        if (entity is None or entity["user_id"] != command.account_user_id
                or entity["current_dispatch_sequence"] != command.subject_sequence):
            raise CommandRefused("job command is no longer current")
        expected_state = "running" if kind == "dispatch_job" else "cancelled"
        if entity["state"] != expected_state:
            raise CommandRefused("job command state is no longer current")
        if not await current_owner(session, command.account_user_id):
            raise CommandRefused("job command owner is no longer current")
        attempt = (await session.execute(
            text(
                "SELECT worker_id, incarnation, range_id, state FROM job_attempts "
                "WHERE job_id = :subject_id AND dispatch_sequence = :sequence FOR UPDATE"
            ), {"subject_id": command.subject_id,
                "sequence": command.subject_sequence},
        )).mappings().one_or_none()
    expected_attempt_state = {
        "open_session": "opening",
        "update_session": "running",
        "close_session": "running",
        "dispatch_job": "dispatching",
        "cancel_job": ("dispatching", "running"),
    }[kind]
    if (attempt is None or attempt["worker_id"] != worker_id
            or attempt["incarnation"] != incarnation
            or (attempt["state"] not in expected_attempt_state
                if isinstance(expected_attempt_state, tuple)
                else attempt["state"] != expected_attempt_state)):
        raise CommandRefused("command attempt is no longer current")
    range_id = attempt["range_id"]
    if range_id is not None:
        range_row = (await session.execute(
            text(
                "SELECT account_user_id, state FROM worker_physical_ranges "
                "WHERE range_id = :range_id FOR UPDATE"
            ), {"range_id": range_id},
        )).mappings().one_or_none()
        if (range_row is None or range_row["account_user_id"] != command.account_user_id
                or range_row["state"] != "outstanding"):
            raise CommandRefused("command physical range is no longer current")

    return binding_deadlines


async def lock_report_subject(
    session, subject_id, user_id, *, is_session, generation, dispatch_sequence,
    worker_id, incarnation, owner_epoch, range_id, attempt_id, report,
):
    entity = (await session.execute(
        text(
            "SELECT id, state FROM realtime_sessions WHERE id = :id AND user_id = :user_id FOR UPDATE"
            if is_session else
            "SELECT id, state, current_dispatch_sequence FROM jobs "
            "WHERE id = :id AND user_id = :user_id FOR UPDATE"
        ),
        {"id": subject_id, "user_id": user_id},
    )).mappings().one_or_none()
    if entity is None:
        return None
    attempt = (await session.execute(
        text(
            "SELECT worker_id, incarnation, owner_epoch, attempt_id, range_id, "
            "gpu_ms, frames, duration_ms, report_sequence FROM realtime_session_attempts "
            "WHERE session_id = :session_id AND control_generation = :generation FOR UPDATE"
            if is_session else
            "SELECT worker_id, incarnation, owner_epoch, dispatch_token_hash, range_id, "
            "gpu_ms, frames, duration_ms, report_sequence FROM job_attempts "
            "WHERE job_id = :job_id AND dispatch_sequence = :dispatch_sequence FOR UPDATE"
        ),
        ({"session_id": subject_id, "generation": generation} if is_session else
         {"job_id": subject_id, "dispatch_sequence": dispatch_sequence}),
    )).mappings().one_or_none()
    if (attempt is None or attempt["worker_id"] != worker_id
            or attempt["incarnation"] != incarnation
            or attempt["owner_epoch"] != owner_epoch
            or attempt["range_id"] != range_id
            or (is_session and attempt["attempt_id"] != attempt_id)):
        return None
    if not is_session and report["type"] in {"job_done", "job_failed", "job_cancelled"}:
        token = report.get("dispatch_token")
        if (not isinstance(token, str) or not token.isascii()
                or not hmac.compare_digest(
                    attempt["dispatch_token_hash"],
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                )):
            return None
    return entity, attempt


def report_ack(report, subject_id, incarnation, range_id, attempt_id, generation,
               dispatch_sequence):
    common = {
        "type": "checkpoint_ack",
        "worker_id": report["worker_id"],
        "incarnation": str(incarnation),
        "region": report["region"],
        "owner_epoch": report["owner_epoch"],
        "range_id": str(range_id),
        "report_sequence": report["report_sequence"],
        "report_hash": report["report_hash"],
        "gpu_ms": report["gpu_ms"],
        "duration_ms": report["duration_ms"],
        "status": "accepted",
    }
    if "session_id" in report:
        common.update({
            "session_id": str(subject_id),
            "attempt_id": str(attempt_id),
            "control_generation": generation,
            "frames": report.get("frames", 0),
        })
    else:
        common.update({
            "job_id": str(subject_id),
            "dispatch_sequence": dispatch_sequence,
            "images": report.get("images", 0),
        })
    return common


async def lock_reported_command(
    session, account_user_id, *, kind, subject_id, subject_sequence, worker_id,
    incarnation, range_id, attempt_id, report_sequence, report_hash,
):
    if (kind not in {"close_session", "dispatch_job"}
            or not isinstance(account_user_id, uuid.UUID)
            or not isinstance(subject_id, uuid.UUID)
            or not isinstance(incarnation, uuid.UUID)
            or not isinstance(range_id, uuid.UUID)
            or not isinstance(subject_sequence, int) or isinstance(subject_sequence, bool)
            or not 1 <= subject_sequence <= MAX_SEQUENCE
            or not isinstance(report_sequence, int) or isinstance(report_sequence, bool)
            or report_sequence < 1 or not isinstance(report_hash, str)
            or len(report_hash) != 64
            or (kind == "close_session") != isinstance(attempt_id, uuid.UUID)):
        raise CommandRefused("reported command identity is invalid")
    if not await session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM worker_report_receipts "
            "WHERE range_id = :range_id AND report_sequence = :report_sequence "
            "AND report_hash = :report_hash)"
        ),
        {"range_id": range_id, "report_sequence": report_sequence,
         "report_hash": report_hash},
    ):
        raise CommandRefused("physical report receipt is unavailable")
    physical = (await session.execute(
        text(
            "SELECT kind, subject_id, attempt_id, control_generation, dispatch_sequence "
            "FROM worker_physical_ranges WHERE range_id = :range_id AND worker_id = :worker_id "
            "AND incarnation = :incarnation"
        ),
        {"range_id": range_id, "worker_id": worker_id, "incarnation": incarnation},
    )).mappings().one_or_none()
    if (physical is None
            or physical["kind"] != ("session" if kind == "close_session" else "job")
            or physical["subject_id"] != subject_id or physical["attempt_id"] != attempt_id
            or physical["control_generation"] != (subject_sequence if kind == "close_session" else None)
            or physical["dispatch_sequence"] != (subject_sequence if kind == "dispatch_job" else None)):
        raise CommandRefused("physical report range identity changed")
    if kind == "close_session":
        proof = (await session.execute(
            text(
                "SELECT session.state AS entity_state, attempt.state AS attempt_state, "
                "attempt.attempt_id, attempt.worker_id, attempt.incarnation, attempt.range_id "
                "FROM realtime_sessions session JOIN realtime_session_attempts attempt "
                "ON attempt.session_id = session.id WHERE session.id = :subject_id "
                "AND session.user_id = :account_user_id "
                "AND session.control_generation = :subject_sequence "
                "AND attempt.control_generation = :subject_sequence"
            ),
            {"subject_id": subject_id, "account_user_id": account_user_id,
             "subject_sequence": subject_sequence},
        )).mappings().one_or_none()
        if (proof is None or proof["entity_state"] != "idle"
                or proof["attempt_state"] != "ended" or proof["attempt_id"] != attempt_id
                or proof["worker_id"] != worker_id or proof["incarnation"] != incarnation
                or proof["range_id"] != range_id):
            raise CommandRefused("session close report is not physically complete")
    else:
        proof = (await session.execute(
            text(
                "SELECT job.state AS entity_state, job.current_dispatch_sequence, "
                "attempt.state AS attempt_state, attempt.worker_id, attempt.incarnation, "
                "attempt.range_id, attempt.terminal_at FROM jobs job "
                "JOIN job_attempts attempt ON attempt.job_id = job.id "
                "WHERE job.id = :subject_id AND job.user_id = :account_user_id "
                "AND job.current_dispatch_sequence = :subject_sequence "
                "AND attempt.dispatch_sequence = :subject_sequence"
            ),
            {"subject_id": subject_id, "account_user_id": account_user_id,
             "subject_sequence": subject_sequence},
        )).mappings().one_or_none()
        if (proof is None or proof["entity_state"] not in {"running", "completed", "failed", "cancelled"}
                or proof["attempt_state"] not in {"completed", "failed", "cancelled"}
                or proof["terminal_at"] is None or proof["worker_id"] != worker_id
                or proof["incarnation"] != incarnation or proof["range_id"] != range_id):
            raise CommandRefused("job terminal report is not physically complete")
    return await lock_reported_subject_rows(
        session, account_user_id, kind=kind, subject_id=subject_id,
        subject_sequence=subject_sequence, worker_id=worker_id,
        incarnation=incarnation,
    )


async def lock_reported_subject_rows(
    session, account_user_id, *, kind, subject_id, subject_sequence, worker_id, incarnation
):
    return (await session.execute(
        text(
            "SELECT command_sequence, command_id, body_hash, key_version, encrypted_body, "
            "account_user_id, subject_id, subject_sequence, ack_identity_hash "
            "FROM worker_commands WHERE account_user_id = :account_user_id "
            "AND worker_id = :worker_id AND incarnation = :incarnation AND kind = :kind "
            "AND subject_id = :subject_id AND subject_sequence = :subject_sequence "
            "AND state = 'pending' ORDER BY worker_id, incarnation, command_sequence FOR UPDATE"
        ),
        {"account_user_id": account_user_id, "worker_id": worker_id,
         "incarnation": incarnation, "kind": kind, "subject_id": subject_id,
         "subject_sequence": subject_sequence},
    )).mappings().all()


async def lock_update_ack_subject(session, hint):
    if hint is None or hint["state"] != "pending" or hint["kind"] != "update_session":
        return None, None, None
    entity = (await session.execute(
        text(
            "SELECT user_id, auth_session_id, browser_owner_id, model_id, "
            "control_generation, state, params_revision FROM realtime_sessions "
            "WHERE id = :subject_id FOR UPDATE"
        ),
        {"subject_id": hint["subject_id"]},
    )).mappings().one_or_none()
    if entity is None or entity["control_generation"] != hint["subject_sequence"]:
        return entity, None, None
    attempt = (await session.execute(
        text(
            "SELECT worker_id, incarnation, attempt_id, range_id, state "
            "FROM realtime_session_attempts WHERE session_id = :subject_id "
            "AND control_generation = :sequence FOR UPDATE"
        ),
        {"subject_id": hint["subject_id"], "sequence": hint["subject_sequence"]},
    )).mappings().one_or_none()
    physical = None
    if attempt is not None and attempt["range_id"] is not None:
        physical = (await session.execute(
            text(
                "SELECT account_user_id, state FROM worker_physical_ranges "
                "WHERE range_id = :range_id FOR UPDATE"
            ),
            {"range_id": attempt["range_id"]},
        )).mappings().one_or_none()
    return entity, attempt, physical


async def read_committed_desired(session_id: uuid.UUID, account_user_id: uuid.UUID,
                                 generation: int):
    if db.session_factory is None:
        return None
    async with db.session_factory() as session:
        await hold_the_account(session, account_user_id, wait=True)
        row = (await session.execute(
            text(
                "SELECT desired_params, desired_revision FROM realtime_sessions "
                "WHERE id = :session_id AND user_id = :user_id "
                "AND control_generation = :generation FOR SHARE"
            ),
            {"session_id": session_id, "user_id": account_user_id,
             "generation": generation},
        )).mappings().one_or_none()
        return (dict(row["desired_params"]), int(row["desired_revision"])) if row else None


async def lock_subject_commands(
    session, account_user_id: uuid.UUID, kind: str, subject_id: uuid.UUID,
    subject_sequence: int | None,
):
    commands = subject_command_kinds(kind)
    if not isinstance(account_user_id, uuid.UUID) or not isinstance(subject_id, uuid.UUID):
        raise CommandRefused("command erasure subject identity is invalid")
    values: dict[str, object] = {
        f"kind_{index}": item for index, item in enumerate(commands)
    }
    values.update(account_user_id=account_user_id, subject_id=subject_id)
    kinds = ", ".join(f":kind_{index}" for index in range(len(commands)))
    sequence_clause = ""
    if subject_sequence is not None:
        if (not isinstance(subject_sequence, int) or isinstance(subject_sequence, bool)
                or not 1 <= subject_sequence <= MAX_SEQUENCE):
            raise CommandRefused("command erasure subject sequence is invalid")
        sequence_clause = " AND subject_sequence = :subject_sequence"
        values["subject_sequence"] = subject_sequence
    return (await session.execute(
        text(
            "SELECT worker_id, incarnation, command_sequence, state FROM worker_commands "
            f"WHERE account_user_id = :account_user_id AND kind IN ({kinds}) "
            "AND subject_id = :subject_id" + sequence_clause +
            " ORDER BY worker_id, incarnation, command_sequence FOR UPDATE"
        ),
        values,
    )).mappings().all()


def subject_command_kinds(kind: str) -> tuple[str, ...]:
    commands = SUBJECT_COMMANDS.get(kind, (kind,))
    if any(command not in SUBJECT_FIELDS for command in commands):
        raise CommandRefused("command erasure subject identity is invalid")
    return commands


async def realtime_erasure_selection(
    session, account_user_id, *, auth_session_ids, keep_auth_session_id
):
    if not isinstance(account_user_id, uuid.UUID):
        raise CommandRefused("command erasure account identity is invalid")
    if auth_session_ids is not None and not auth_session_ids:
        return None
    bindings = (await session.execute(
        text(
            "SELECT id FROM realtime_sessions WHERE user_id = :account_user_id "
            "AND (CAST(:auth_ids AS uuid[]) IS NULL OR "
            "auth_session_id = ANY(CAST(:auth_ids AS uuid[]))) "
            "AND (CAST(:keep_auth_session_id AS uuid) IS NULL OR "
            "auth_session_id IS DISTINCT FROM CAST(:keep_auth_session_id AS uuid)) "
            "ORDER BY id FOR UPDATE"
        ),
        {"account_user_id": account_user_id,
         "auth_ids": list(auth_session_ids) if auth_session_ids is not None else None,
         "keep_auth_session_id": keep_auth_session_id},
    )).scalars().all()
    if not bindings:
        return None
    workers = await lock_subject_attempts(
        session, account_user_id, kind="session", subject_ids=bindings
    )
    return (
        "account_user_id = :account_user_id AND kind IN ('open_session','update_session',"
        "'close_session') AND subject_id = ANY(:subject_ids)",
        {"account_user_id": account_user_id, "subject_ids": bindings}, workers, None,
    )


async def lock_attempt_ranges(session, attempts, account_ids: Collection[uuid.UUID]):
    account_ids = list(account_ids)
    range_ids = sorted({row["range_id"] for row in attempts if row["range_id"] is not None})
    for range_id in range_ids:
        await session.execute(
            text(
                "SELECT range_id FROM worker_physical_ranges "
                "WHERE range_id = :range_id "
                "AND account_user_id = ANY(CAST(:account_user_ids AS uuid[])) FOR UPDATE"
            ),
            {"range_id": range_id, "account_user_ids": account_ids},
        )
    return {(row["worker_id"], row["incarnation"]) for row in attempts}


async def account_erasure_selection(session, account_user_ids: Collection[uuid.UUID]):
    values = {"account_user_ids": list(account_user_ids)}
    sessions = (await session.execute(
        text(
            "SELECT session.id, attempt.worker_id, attempt.incarnation, attempt.range_id "
            "FROM realtime_sessions session JOIN realtime_session_attempts attempt "
            "ON attempt.session_id = session.id JOIN worker_physical_ranges physical "
            "ON physical.range_id = attempt.range_id "
            "AND physical.account_user_id = ANY(CAST(:account_user_ids AS uuid[])) "
            "AND physical.kind = 'session' AND physical.subject_id = session.id "
            "AND physical.attempt_id = attempt.attempt_id "
            "AND physical.control_generation = attempt.control_generation "
            "WHERE session.state IN ('assigning','live','idle','ending') "
            "AND attempt.state IN ('opening','running') "
            "ORDER BY session.id, attempt.control_generation FOR UPDATE OF session, attempt"
        ),
        values,
    )).mappings().all()
    jobs = (await session.execute(
        text(
            "SELECT job.id, attempt.worker_id, attempt.incarnation, attempt.range_id "
            "FROM jobs job JOIN job_attempts attempt ON attempt.job_id = job.id "
            "AND attempt.dispatch_sequence = job.current_dispatch_sequence "
            "JOIN worker_physical_ranges physical ON physical.range_id = attempt.range_id "
            "AND physical.account_user_id = ANY(CAST(:account_user_ids AS uuid[])) "
            "AND physical.kind = 'job' "
            "AND physical.subject_id = job.id AND physical.dispatch_sequence = attempt.dispatch_sequence "
            "WHERE job.state IN ('running','cancelled') AND attempt.state = 'dispatching' "
            "ORDER BY job.id, attempt.dispatch_sequence FOR UPDATE OF job, attempt"
        ),
        values,
    )).mappings().all()
    return (
        "account_user_id = ANY(CAST(:account_user_ids AS uuid[]))", values,
        await lock_attempt_ranges(session, [*sessions, *jobs], account_user_ids),
    )


async def subject_erasure_selection(
    session, account_user_id, *, kind, subject_id, subject_sequence
):
    if not isinstance(account_user_id, uuid.UUID) or not isinstance(subject_id, uuid.UUID):
        raise CommandRefused("command erasure subject identity is invalid")
    kinds = subject_command_kinds(kind)
    values: dict[str, object] = {
        f"kind_{index}": item for index, item in enumerate(kinds)
    }
    values.update(account_user_id=account_user_id, subject_id=subject_id)
    statement = (
        "account_user_id = :account_user_id AND kind IN ("
        + ", ".join(f":kind_{index}" for index in range(len(kinds)))
        + ") AND subject_id = :subject_id"
    )
    if subject_sequence is not None:
        if (not isinstance(subject_sequence, int) or isinstance(subject_sequence, bool)
                or not 1 <= subject_sequence <= MAX_SEQUENCE):
            raise CommandRefused("command erasure subject sequence is invalid")
        values["subject_sequence"] = subject_sequence
        statement += " AND subject_sequence = :subject_sequence"
    workers = await lock_subject_attempts(
        session, account_user_id,
        kind="session" if all(item.endswith("_session") for item in kinds) else "job",
        subject_id=subject_id, subject_sequence=subject_sequence,
    )
    rows = await lock_subject_commands(
        session, account_user_id, kind, subject_id, subject_sequence
    )
    return statement, values, workers, rows


async def lock_subject_attempts(
    session, account_user_id: uuid.UUID, *, kind: str,
    subject_id: uuid.UUID | None = None, subject_ids: list[uuid.UUID] | None = None,
    subject_sequence: int | None = None,
) -> set[tuple[str, uuid.UUID]]:
    values: dict[str, object] = {"account_user_id": account_user_id}
    if kind == "session":
        selector = "session.id = :subject_id" if subject_id is not None else (
            "session.id = ANY(CAST(:subject_ids AS uuid[]))"
        )
        if subject_id is not None:
            values["subject_id"] = subject_id
        else:
            values["subject_ids"] = subject_ids
        sequence = ""
        if subject_sequence is not None:
            sequence = "AND attempt.control_generation = :subject_sequence "
            values["subject_sequence"] = subject_sequence
        attempts = (await session.execute(
            text(
                "SELECT session.id, attempt.worker_id, attempt.incarnation, attempt.range_id "
                "FROM realtime_sessions session JOIN realtime_session_attempts attempt "
                "ON attempt.session_id = session.id JOIN worker_physical_ranges physical "
                "ON physical.range_id = attempt.range_id AND physical.account_user_id = :account_user_id "
                "AND physical.kind = 'session' AND physical.subject_id = session.id "
                "AND physical.attempt_id = attempt.attempt_id "
                "AND physical.control_generation = attempt.control_generation "
                f"WHERE {selector} {sequence}"
                "AND session.state IN ('assigning','live','idle','ending') "
                "AND attempt.state IN ('opening','running') "
                "ORDER BY session.id, attempt.control_generation FOR UPDATE OF session, attempt"
            ), values,
        )).mappings().all()
    else:
        selector = "job.id = :subject_id" if subject_id is not None else "TRUE"
        if subject_id is not None:
            values["subject_id"] = subject_id
        sequence = ""
        if subject_sequence is not None:
            sequence = "AND attempt.dispatch_sequence = :subject_sequence "
            values["subject_sequence"] = subject_sequence
        attempts = (await session.execute(
            text(
                "SELECT job.id, attempt.worker_id, attempt.incarnation, attempt.range_id "
                "FROM jobs job JOIN job_attempts attempt ON attempt.job_id = job.id "
                "JOIN worker_physical_ranges physical ON physical.range_id = attempt.range_id "
                "AND physical.account_user_id = :account_user_id AND physical.kind = 'job' "
                "AND physical.subject_id = job.id AND physical.dispatch_sequence = attempt.dispatch_sequence "
                "WHERE " + selector + " " + sequence +
                "AND job.state IN ('running','cancelled') AND attempt.state = 'dispatching' "
                "ORDER BY job.id, attempt.dispatch_sequence FOR UPDATE OF job, attempt"
            ), values,
        )).mappings().all()
    return await lock_attempt_ranges(session, attempts, {account_user_id})
