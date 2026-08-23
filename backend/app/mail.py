"""Optional mail, queued in PostgreSQL and delivered by a sweep.

EMAIL_BACKEND=none is the self-hosted default: nothing is ever sent and the
invitation link is copied by hand. When a backend is configured the capability
is written to the outbox in the same transaction that mints it, so a delivery
outage queues and retries instead of losing it, and never stops anyone from
logging in.
"""

import asyncio
import logging
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from anyio import to_thread
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.settings import Settings, get_settings
from app.tables import MailOutbox, SuppressedAddress

logger = logging.getLogger("potocolom.mail")

MAX_ATTEMPTS = 6
BACKOFF = timedelta(seconds=60)
BATCH_LIMIT = 100
SWEEP_INTERVAL = 30.0
SMTP_TIMEOUT = 10.0

REQUIRED = {
    "smtp": (("SMTP_HOST", "smtp_host"), ("MAIL_FROM", "mail_from")),
    "ses": (("MAIL_FROM", "mail_from"), ("SES_REGION", "ses_region")),
}

SUBJECTS = {"invitation": "You have been invited to potocolom"}
DEFAULT_SUBJECT = "potocolom"

# Only the code that means this address is on the provider's own suppression
# list. MessageRejected and InvalidParameterValue are account and
# configuration problems: in the SES sandbox every unverified recipient
# returns MessageRejected, which would retire every address an operator
# invites before they leave it.
SES_PERMANENT = ("AccountSuppressionListException",)


class PermanentlyUndeliverable(Exception):
    """The relay refused the address, not the connection."""


def check_configuration(settings: Settings) -> None:
    """Refuse a mail configuration that cannot deliver, or cannot deliver safely.

    An install that believes invitations are going out is worse off than one
    whose API will not boot.
    """
    if settings.email_backend == "none":
        return
    if settings.email_backend == "smtp":
        if not settings.smtp_host:
            raise RuntimeError("EMAIL_BACKEND=smtp needs SMTP_HOST")
        if not 1 <= settings.smtp_port <= 65535:
            raise RuntimeError(f"SMTP_PORT must be between 1 and 65535, not {settings.smtp_port}")
        if not settings.smtp_starttls and not _loopback(settings.smtp_host):
            # The message carries a capability and the login carries the
            # password. In the clear to another host, both are readable by
            # anyone on the path. A relay on this machine is the operator's
            # own loopback and their decision.
            raise RuntimeError(
                "SMTP_STARTTLS=false is only allowed for a relay on localhost; "
                f"{settings.smtp_host} is not one"
            )
    if not settings.mail_from:
        raise RuntimeError(f"EMAIL_BACKEND={settings.email_backend} needs MAIL_FROM")
    if settings.email_backend == "ses" and not settings.ses_region:
        raise RuntimeError("EMAIL_BACKEND=ses needs SES_REGION")
    if not _safe_link_origin(settings.public_url):
        # The mail carries a link whose fragment is the capability. Over plain
        # HTTP anyone on the path reads it, and script on the page can too.
        raise RuntimeError(
            "sending mail needs an https PUBLIC_URL, because the invitation "
            f"link is the capability; {settings.public_url} is not one"
        )


def _loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"}


def _safe_link_origin(public_url: str) -> bool:
    if public_url.startswith("https://"):
        return True
    host = public_url.split("//", 1)[-1].split("/")[0].split(":")[0]
    return _loopback(host)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(to_email: str) -> str:
    return to_email.strip().lower()


async def queue(session: AsyncSession, to_email: str, template: str, payload: dict) -> bool:
    """Adds the row to the caller's transaction without committing, so the
    capability and its delivery become durable together or not at all."""
    if get_settings().email_backend == "none":
        return False
    if await _suppressed(session, to_email):
        return False
    session.add(MailOutbox(
        to_email=to_email,
        template=template,
        payload=payload,
        next_attempt_at=_now(),
    ))
    return True


async def _suppressed(session: AsyncSession, to_email: str) -> bool:
    found = await session.execute(
        select(SuppressedAddress.email).where(SuppressedAddress.email == _normalize(to_email))
    )
    return found.scalar_one_or_none() is not None


async def is_suppressed(to_email: str) -> bool:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        return await _suppressed(session, to_email)


async def suppress(to_email: str, reason: str) -> None:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        await session.execute(
            insert(SuppressedAddress)
            .values(email=_normalize(to_email), reason=reason)
            .on_conflict_do_nothing(index_elements=[SuppressedAddress.email])
        )
        await session.commit()


async def deliver_due() -> None:
    """Never raises, and never lets one row decide the fate of the others."""
    if db.session_factory is None:
        return
    settings = get_settings()
    try:
        async with db.session_factory() as session:
            pending = select(MailOutbox).where(MailOutbox.state == "pending")
            if settings.email_backend != "none":
                pending = pending.where(MailOutbox.next_attempt_at <= _now())
            # With no backend there is nothing to wait for: a row backed off
            # sixteen minutes would otherwise keep its live link that whole
            # time, and longer if the process stops first.
            due = (await session.execute(
                pending.order_by(MailOutbox.next_attempt_at).limit(BATCH_LIMIT)
            )).scalars().all()
            ids = [row.id for row in due]
    except Exception:
        logger.exception("could not read the mail outbox")
        return
    for row_id in ids:
        await _deliver_one(row_id, settings)


async def _deliver_one(row_id: uuid.UUID, settings: Settings) -> None:
    """One row, one transaction, one commit.

    A batch-wide transaction re-delivered everything it had already sent when
    anything later in the batch failed, and never advanced attempts, so a
    poison batch could repeat without end. Taking the row with SKIP LOCKED is
    what stops a second process delivering the same capability.
    """
    if db.session_factory is None:
        return
    try:
        async with db.session_factory() as session:
            async with session.begin():
                row = (await session.execute(
                    select(MailOutbox)
                    .where(MailOutbox.id == row_id, MailOutbox.state == "pending")
                    .with_for_update(skip_locked=True)
                )).scalar_one_or_none()
                if row is None:
                    return
                if settings.email_backend == "none":
                    # Switched off with rows still queued. Nothing can ever
                    # deliver them, and each one still holds a live
                    # capability, so settle them rather than leave them.
                    row.state = "failed"
                    row.last_error = "no mail backend configured"
                    _forget_capability(row)
                    return
                await _attempt(row, settings)
    except Exception:
        logger.exception("could not settle a mail outbox row")


async def mail_loop() -> None:
    """Sleeps first. A sweep on startup makes every API start do database work
    before it serves anything, and nothing is waiting that was not waiting a
    moment earlier."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        await deliver_due()


async def _attempt(row: MailOutbox, settings: Settings) -> None:
    row.attempts += 1
    try:
        await _deliver(row, settings)
    except PermanentlyUndeliverable as error:
        row.state = "failed"
        row.last_error = str(error)
        _forget_capability(row)
        await suppress(row.to_email, "undeliverable")
    except Exception as error:
        row.last_error = str(error)
        row.next_attempt_at = _now() + BACKOFF * 2 ** (row.attempts - 1)
        if row.attempts >= MAX_ATTEMPTS:
            row.state = "failed"
            _forget_capability(row)
    else:
        row.state = "sent"
        row.sent_at = _now()
        _forget_capability(row)


def _forget_capability(row: MailOutbox) -> None:
    """The payload carries a live capability, an invitation link today.

    A settled row does not need it, and keeping it would leave a bearer token
    in this table, and in every backup of it, for as long as the row lives.
    What the mail was and who it was for stay, so an operator can still act.
    """
    row.payload = {}


async def status() -> dict:
    if db.session_factory is None:
        raise RuntimeError("database unavailable")
    async with db.session_factory() as session:
        counts = {
            state: count
            for state, count in (await session.execute(
                select(MailOutbox.state, func.count()).group_by(MailOutbox.state)
            )).all()
        }
        oldest = (await session.execute(
            select(func.min(MailOutbox.created_at)).where(MailOutbox.state == "pending")
        )).scalar_one()
    return {
        "backend": get_settings().email_backend,
        "sent": counts.get("sent", 0),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "oldest_pending_at": oldest.isoformat() if oldest is not None else None,
    }


async def _deliver(row: MailOutbox, settings: Settings) -> None:
    if settings.email_backend == "smtp":
        await to_thread.run_sync(_send_smtp, row, settings)
    elif settings.email_backend == "ses":
        await to_thread.run_sync(_send_ses, row, settings)
    else:
        # Never silently succeed: the caller marks a row sent on return, and a
        # row marked sent that nobody sent is a capability nobody received.
        raise RuntimeError(f"no mail backend to deliver with: {settings.email_backend}")


def _render(row: MailOutbox) -> tuple[str, str]:
    subject = SUBJECTS.get(row.template, DEFAULT_SUBJECT)
    return subject, f"{subject}\n\n{row.payload.get('link', '')}\n"


def _permanently_refused(recipients: dict) -> bool:
    """True only when every recipient was refused with a 5xx.

    A sender-side refusal is not here at all: MAIL FROM failing is a quota or
    a configuration problem, and says nothing about who it was addressed to.
    """
    codes = [code for code, _ in recipients.values()]
    return bool(codes) and all(500 <= code < 600 for code in codes)


def _send_smtp(row: MailOutbox, settings: Settings) -> None:
    subject, body = _render(row)
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = row.to_email
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT) as relay:
            if settings.smtp_starttls:
                # Without a context smtplib builds one with no certificate check
                # at all, so anyone on the path to the relay reads the
                # credentials below and the capability in the body.
                relay.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                relay.login(settings.smtp_username, settings.smtp_password)
            relay.send_message(message)
    except smtplib.SMTPRecipientsRefused as error:
        # Only a 5xx says the address is wrong. A 4xx is greylisting, a full
        # mailbox or a busy relay, and suppressing on those retires a working
        # address for good, with nothing in this codebase to bring it back.
        if _permanently_refused(error.recipients):
            raise PermanentlyUndeliverable(str(error)) from error
        raise


def _send_ses(row: MailOutbox, settings: Settings) -> None:
    import boto3
    from botocore.exceptions import ClientError

    subject, body = _render(row)
    client = boto3.client("sesv2", region_name=settings.ses_region)
    try:
        client.send_email(
            FromEmailAddress=settings.mail_from,
            Destination={"ToAddresses": [row.to_email]},
            Content={"Simple": {
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            }},
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in SES_PERMANENT:
            raise PermanentlyUndeliverable(f"{code}: {row.to_email}") from error
        raise
