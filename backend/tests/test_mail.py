from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app import db, mail
from app.settings import Settings, get_settings
from app.tables import MailOutbox, SuppressedAddress

TO = "Invited@Example.com"


@pytest.fixture
def connected(portal_runner):
    assert portal_runner(db.connect()) is True

    async def clear() -> None:
        async with db.session_factory() as session:
            await session.execute(text("DELETE FROM mail_outbox"))
            await session.execute(text("DELETE FROM suppressed_addresses"))
            await session.commit()

    portal_runner(clear())
    try:
        yield portal_runner
    finally:
        portal_runner(clear())
        portal_runner(db.dispose())


async def _rows() -> list[MailOutbox]:
    async with db.session_factory() as session:
        return list((await session.execute(
            select(MailOutbox).order_by(MailOutbox.created_at)
        )).scalars().all())


async def _queue(to_email=TO, template="invitation", payload=None) -> bool:
    async with db.session_factory() as session:
        queued = await mail.queue(session, to_email, template, payload or {"link": "x"})
        await session.commit()
        return queued


def test_the_configured_backends_are_none_smtp_and_ses():
    assert Settings().email_backend == "none"
    assert Settings(email_backend="smtp").email_backend == "smtp"
    assert Settings(email_backend="ses").email_backend == "ses"


@pytest.mark.parametrize("settings, missing", [
    ({"email_backend": "smtp"}, "SMTP_HOST"),
    ({"email_backend": "smtp", "smtp_host": "mail.example.com"}, "MAIL_FROM"),
    ({"email_backend": "ses"}, "MAIL_FROM"),
    ({"email_backend": "ses", "mail_from": "a@b.co"}, "SES_REGION"),
])
def test_a_structurally_incomplete_mail_configuration_refuses_to_start(settings, missing):
    """An install configured for mail that cannot send is worse than one with
    no mail: the operator believes invitations are going out."""
    with pytest.raises(RuntimeError, match=missing):
        mail.check_configuration(Settings(**settings))


def test_a_complete_configuration_and_no_mail_at_all_both_start():
    mail.check_configuration(Settings())
    mail.check_configuration(Settings(email_backend="smtp", smtp_host="mail.example.com",
                                      mail_from="potocolom@example.com"))
    mail.check_configuration(Settings(email_backend="ses", mail_from="potocolom@example.com",
                                      ses_region="eu-west-1"))


@pytest.mark.db
def test_with_no_backend_nothing_is_queued(connected):
    """Mail stays optional. The invitation link is copied by hand instead, so
    a queue nobody drains would only grow."""
    assert connected(_queue()) is False
    assert connected(_rows()) == []


@pytest.mark.db
def test_a_capability_is_durable_before_anyone_tries_to_send_it(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    assert connected(_queue()) is True
    row = connected(_rows())[0]
    assert row.to_email == TO
    assert row.template == "invitation"
    assert row.state == "pending"
    assert row.attempts == 0
    assert row.sent_at is None


def _configure(monkeypatch, backend):
    monkeypatch.setenv("EMAIL_BACKEND", backend)
    monkeypatch.setenv("MAIL_FROM", "potocolom@example.com")
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SES_REGION", "eu-west-1")
    get_settings.cache_clear()


@pytest.mark.db
def test_a_delivered_capability_is_marked_sent_once(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    sent = []
    monkeypatch.setattr(mail, "_deliver", _record(sent))
    connected(_queue())
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "sent" and row.sent_at is not None
    assert len(sent) == 1
    # A second sweep must not send it again.
    connected(mail.deliver_due())
    assert len(sent) == 1


def _record(sink, error=None):
    async def deliver(row, settings):
        sink.append(row.to_email)
        if error is not None:
            raise error

    return deliver


@pytest.mark.db
def test_a_delivery_outage_retries_with_a_widening_gap(connected, monkeypatch):
    """The outage must not lose the capability, and must not hammer the relay
    that is already failing."""
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver", _record([], error=OSError("connection refused")))
    connected(_queue())
    gaps = []
    for _ in range(3):
        connected(_due_now())
        connected(mail.deliver_due())
        row = connected(_rows())[0]
        gaps.append(row.next_attempt_at - datetime.now(timezone.utc))
    assert [row.state for row in connected(_rows())] == ["pending"]
    assert connected(_rows())[0].attempts == 3
    assert "connection refused" in connected(_rows())[0].last_error
    assert gaps[0] < gaps[1] < gaps[2]


async def _due_now() -> None:
    async with db.session_factory() as session:
        await session.execute(text("UPDATE mail_outbox SET next_attempt_at = :now"),
                              {"now": datetime.now(timezone.utc)})
        await session.commit()


@pytest.mark.db
def test_a_capability_that_keeps_failing_stops_being_retried(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver", _record([], error=OSError("still down")))
    connected(_queue())
    for _ in range(mail.MAX_ATTEMPTS):
        connected(_due_now())
        connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "failed"
    assert row.attempts == mail.MAX_ATTEMPTS


@pytest.mark.db
def test_an_address_the_relay_rejects_outright_is_suppressed(connected, monkeypatch):
    """A permanent rejection is the address being wrong, not the relay being
    down, so retrying it only teaches the relay to distrust us."""
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver",
                        _record([], error=mail.PermanentlyUndeliverable("no such mailbox")))
    connected(_queue())
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "failed"
    assert connected(mail.is_suppressed(TO)) is True
    # Case and padding must not walk past the suppression.
    assert connected(mail.is_suppressed("  invited@EXAMPLE.com ")) is True


@pytest.mark.db
def test_a_suppressed_address_is_never_queued_again(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    connected(mail.suppress(TO, "bounce"))
    assert connected(_queue()) is False
    assert connected(_rows()) == []


@pytest.mark.db
def test_feedback_from_the_provider_suppresses_the_address(connected, monkeypatch):
    _configure(monkeypatch, "ses")
    connected(mail.suppress("Bounced@Example.com", "complaint"))

    async def stored() -> SuppressedAddress:
        async with db.session_factory() as session:
            return (await session.execute(select(SuppressedAddress))).scalar_one()

    row = connected(stored())
    assert row.email == "bounced@example.com"
    assert row.reason == "complaint"
    # Recording it twice is what a provider retrying its notification does.
    connected(mail.suppress("bounced@example.com", "bounce"))
    assert len(connected(_all_suppressed())) == 1


async def _all_suppressed() -> list[SuppressedAddress]:
    async with db.session_factory() as session:
        return list((await session.execute(select(SuppressedAddress))).scalars().all())


@pytest.mark.db
def test_the_operator_can_see_what_the_queue_is_doing(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver", _record([]))
    connected(_queue("first@example.com"))
    connected(mail.deliver_due())
    monkeypatch.setattr(mail, "_deliver", _record([], error=OSError("down")))
    connected(_queue("second@example.com"))
    connected(mail.deliver_due())
    status = connected(mail.status())
    assert status["backend"] == "smtp"
    assert status["sent"] == 1
    assert status["pending"] == 1
    assert status["oldest_pending_at"] is not None


@pytest.mark.db
def test_a_sweep_survives_one_capability_that_cannot_be_sent(connected, monkeypatch):
    """One bad address must not stop the queue behind it."""
    _configure(monkeypatch, "smtp")
    sent = []

    async def deliver(row, settings):
        if row.to_email == "bad@example.com":
            raise OSError("refused")
        sent.append(row.to_email)

    monkeypatch.setattr(mail, "_deliver", deliver)
    connected(_queue("bad@example.com"))
    connected(_queue("good@example.com"))
    connected(mail.deliver_due())
    assert sent == ["good@example.com"]
