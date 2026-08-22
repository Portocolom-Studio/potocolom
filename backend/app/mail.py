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

SES_PERMANENT = ("MessageRejected", "AccountSuppressionListException", "InvalidParameterValue")


class PermanentlyUndeliverable(Exception):
    """The relay refused the address, not the connection."""


def check_configuration(settings: Settings) -> None:
    """An install that believes it sends mail and cannot is worse than one that
    never claimed to, so an incomplete backend refuses to start."""
    for variable, field in REQUIRED.get(settings.email_backend, ()):
        if not getattr(settings, field):
            raise RuntimeError(f"EMAIL_BACKEND={settings.email_backend} requires {variable}")


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
    """Never raises. A sweep that dies on one row stops every row behind it."""
    if db.session_factory is None:
        return
    settings = get_settings()
    try:
        async with db.session_factory() as session:
            due = (await session.execute(
                select(MailOutbox)
                .where(MailOutbox.state == "pending", MailOutbox.next_attempt_at <= _now())
                .order_by(MailOutbox.next_attempt_at)
                .limit(BATCH_LIMIT)
            )).scalars().all()
            for row in due:
                await _attempt(row, settings)
            await session.commit()
    except Exception as error:
        logger.warning("mail sweep failed: %s", error)


async def mail_loop() -> None:
    if get_settings().email_backend == "none":
        return
    while True:
        await deliver_due()
        await asyncio.sleep(SWEEP_INTERVAL)


async def _attempt(row: MailOutbox, settings: Settings) -> None:
    row.attempts += 1
    try:
        await _deliver(row, settings)
    except PermanentlyUndeliverable as error:
        row.state = "failed"
        row.last_error = str(error)
        await suppress(row.to_email, "undeliverable")
    except Exception as error:
        row.last_error = str(error)
        row.next_attempt_at = _now() + BACKOFF * 2 ** (row.attempts - 1)
        if row.attempts >= MAX_ATTEMPTS:
            row.state = "failed"
    else:
        row.state = "sent"
        row.sent_at = _now()


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


def _render(row: MailOutbox) -> tuple[str, str]:
    subject = SUBJECTS.get(row.template, DEFAULT_SUBJECT)
    return subject, f"{subject}\n\n{row.payload.get('link', '')}\n"


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
                relay.starttls()
            if settings.smtp_username:
                relay.login(settings.smtp_username, settings.smtp_password)
            relay.send_message(message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as error:
        raise PermanentlyUndeliverable(str(error)) from error


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
