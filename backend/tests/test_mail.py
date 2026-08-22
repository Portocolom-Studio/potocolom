import asyncio
import smtplib
import ssl
import uuid
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


@pytest.mark.db
def test_a_settled_row_stops_holding_the_capability_it_carried(connected, monkeypatch):
    """The payload holds a live invitation link. Queuing it is the point, and
    keeping it after the row is settled is a bearer capability sitting in a
    table, and in every backup of that table, for as long as the row lives."""
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver", _record([]))
    connected(_queue(payload={"link": "https://example.com/join#a-live-token"}))
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "sent"
    assert row.payload == {}
    assert "a-live-token" not in str(row.payload)


@pytest.mark.db
def test_a_failed_row_stops_holding_it_too(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver",
                        _record([], error=mail.PermanentlyUndeliverable("no such mailbox")))
    connected(_queue(payload={"link": "https://example.com/join#another-token"}))
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "failed"
    assert row.payload == {}
    # What it was and who it was for survive, so an operator can still act.
    assert row.to_email == TO and row.template == "invitation"
    assert "no such mailbox" in row.last_error


@pytest.mark.db
def test_a_pending_row_keeps_it_because_it_still_needs_it(connected, monkeypatch):
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver", _record([], error=OSError("relay down")))
    connected(_queue(payload={"link": "https://example.com/join#still-needed"}))
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "pending"
    assert row.payload["link"].endswith("still-needed")



@pytest.mark.parametrize("codes, permanent", [
    ({"a@b.co": (550, b"no such user")}, True),
    ({"a@b.co": (451, b"greylisted, try again")}, False),
    ({"a@b.co": (452, b"mailbox full")}, False),
    ({"a@b.co": (421, b"service unavailable")}, False),
    ({"a@b.co": (550, b"gone"), "c@d.co": (451, b"later")}, False),
    ({}, False),
])
def test_only_a_five_hundred_refusal_means_the_address_is_wrong(codes, permanent):
    """Greylisting is the single most common first answer from a real relay.
    Reading it as a permanent rejection retires a working address for good,
    and nothing in this codebase brings one back."""
    assert mail._permanently_refused(codes) is permanent


@pytest.mark.db
def test_a_sender_side_refusal_leaves_the_recipient_alone(connected, monkeypatch):
    """MAIL FROM failing is a quota or configuration problem and says nothing
    about who the mail was for. Suppressing on it would retire every address
    the install writes to, one per sweep."""
    _configure(monkeypatch, "smtp")
    refusal = smtplib.SMTPSenderRefused(550, b"sender rejected", "potocolom@example.com")
    monkeypatch.setattr(mail, "_deliver", _record([], error=refusal))
    connected(_queue())
    connected(mail.deliver_due())
    assert connected(_rows())[0].state == "pending"
    assert connected(mail.is_suppressed(TO)) is False


def test_the_provider_suppression_list_is_the_only_permanent_ses_code():
    """In the SES sandbox every unverified recipient answers MessageRejected,
    so treating it as permanent retires every address before launch."""
    assert mail.SES_PERMANENT == ("AccountSuppressionListException",)


def test_starttls_verifies_the_relay_certificate(monkeypatch):
    """Without a context smtplib builds one that checks no certificate at all,
    so anyone on the path reads the credentials and the capability."""
    seen = {}

    class FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def starttls(self, context=None):
            seen["context"] = context

        def login(self, *_args):
            pass

        def send_message(self, message):
            seen["to"] = message["To"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    row = MailOutbox(id=uuid.uuid4(), to_email=TO, template="invitation",
                     payload={"link": "https://example.com/join#t"},
                     next_attempt_at=datetime.now(timezone.utc))
    mail._send_smtp(row, Settings(email_backend="smtp", smtp_host="mail.example.com",
                                  mail_from="potocolom@example.com",
                                  public_url="https://studio.example.com"))
    context = seen["context"]
    assert context is not None
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.db
def test_two_sweeps_at_once_deliver_one_capability_once(connected, monkeypatch):
    """Two API tasks share the outbox. Without taking the row, both read it
    and the same bearer link is mailed twice."""
    _configure(monkeypatch, "smtp")
    sent = []

    async def slow(row, settings):
        await asyncio.sleep(0.2)
        sent.append(row.to_email)

    monkeypatch.setattr(mail, "_deliver", slow)
    connected(_queue())

    async def both():
        await asyncio.gather(mail.deliver_due(), mail.deliver_due())

    connected(both())
    assert sent == [TO]
    assert connected(_rows())[0].state == "sent"


@pytest.mark.db
def test_a_row_that_fails_does_not_undo_the_rows_already_sent(connected, monkeypatch):
    """One transaction across the batch rolled back everything it had already
    delivered, and never advanced attempts, so a poison batch repeated."""
    _configure(monkeypatch, "smtp")
    sent = []

    async def deliver(row, settings):
        if row.to_email == "boom@example.com":
            raise OSError("relay died mid-batch")
        sent.append(row.to_email)

    monkeypatch.setattr(mail, "_deliver", deliver)
    connected(_queue("first@example.com"))
    connected(_queue("boom@example.com"))
    connected(mail.deliver_due())
    states = {row.to_email: row.state for row in connected(_rows())}
    assert states["first@example.com"] == "sent"
    assert states["boom@example.com"] == "pending"
    assert [row.attempts for row in connected(_rows()) if row.to_email == "boom@example.com"] == [1]


@pytest.mark.db
def test_switching_mail_off_settles_what_was_queued(connected, monkeypatch):
    """Nothing can ever deliver those rows, and each still holds a live link."""
    _configure(monkeypatch, "smtp")
    connected(_queue(payload={"link": "https://example.com/join#orphaned"}))
    monkeypatch.setenv("EMAIL_BACKEND", "none")
    get_settings.cache_clear()
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "failed"
    assert row.payload == {}


@pytest.mark.db
def test_an_unknown_backend_never_counts_as_delivered(connected, monkeypatch):
    """The caller marks a row sent when _deliver returns. A backend it does
    not recognise must raise, not return."""
    _configure(monkeypatch, "smtp")
    connected(_queue())

    async def go():
        async with db.session_factory() as session:
            row = (await session.execute(select(MailOutbox))).scalar_one()
            with pytest.raises(RuntimeError):
                await mail._deliver(row, Settings(email_backend="none"))

    connected(go())


@pytest.mark.parametrize("settings, missing", [
    ({"email_backend": "smtp", "smtp_host": "mail.example.com",
      "mail_from": "a@b.co", "smtp_port": 0}, "SMTP_PORT"),
    ({"email_backend": "smtp", "smtp_host": "mail.example.com",
      "mail_from": "a@b.co", "smtp_port": 65536}, "SMTP_PORT"),
])
def test_a_port_that_cannot_be_dialled_refuses_to_start(settings, missing):
    with pytest.raises(RuntimeError, match=missing):
        mail.check_configuration(Settings(**settings))


def test_plaintext_smtp_is_only_allowed_to_a_relay_on_this_machine():
    """The message carries a capability and the login carries the password.
    In the clear to another host, both are readable by anyone on the path."""
    with pytest.raises(RuntimeError, match="localhost"):
        mail.check_configuration(Settings(
            email_backend="smtp", smtp_host="mail.example.com",
            mail_from="a@b.co", smtp_starttls=False))
    mail.check_configuration(Settings(
        email_backend="smtp", smtp_host="127.0.0.1",
        mail_from="a@b.co", smtp_starttls=False))


def test_mail_needs_an_https_public_url_because_the_link_is_the_capability():
    """Over plain HTTP the fragment is readable on the wire, and by script on
    the page the recipient lands on."""
    with pytest.raises(RuntimeError, match="https"):
        mail.check_configuration(Settings(
            email_backend="smtp", smtp_host="mail.example.com",
            mail_from="a@b.co", public_url="http://studio.example.com"))
    mail.check_configuration(Settings(
        email_backend="smtp", smtp_host="mail.example.com",
        mail_from="a@b.co", public_url="https://studio.example.com"))
    # The dev loop is the exception, and only for a loopback host.
    mail.check_configuration(Settings(
        email_backend="smtp", smtp_host="mail.example.com",
        mail_from="a@b.co", public_url="http://localhost:8000"))


@pytest.mark.db
def test_switching_mail_off_reaches_a_row_that_is_backed_off(connected, monkeypatch):
    """The due-time filter would leave a retried row holding its live link for
    the whole backoff, and longer if the process stops first."""
    _configure(monkeypatch, "smtp")
    monkeypatch.setattr(mail, "_deliver", _record([], error=OSError("down")))
    connected(_queue(payload={"link": "https://example.com/join#backed-off"}))
    connected(mail.deliver_due())
    assert connected(_rows())[0].next_attempt_at > datetime.now(timezone.utc)

    monkeypatch.setenv("EMAIL_BACKEND", "none")
    get_settings.cache_clear()
    connected(mail.deliver_due())
    row = connected(_rows())[0]
    assert row.state == "failed"
    assert row.payload == {}
