"""The provider's own word about an address, and what it takes to be believed.

Suppression is a denial of mail to a real person: an address that lands here
stops receiving invitations and reset links until an operator removes it. So
every property this suite pins is about refusing, and the one case that
suppresses is the one where SNS signed the message and the topic is ours.
"""

import base64
import datetime
import json
from contextlib import contextmanager

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, ses_feedback
from app.main import app
from app.settings import get_settings
from app.tables import SuppressedAddress

TOPIC = "arn:aws:sns:eu-west-1:123456789012:potocolom-ses-feedback"
CERT_URL = "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-abc123.pem"
BOUNCED = "Bounced@Example.com"


@pytest.fixture
def signing():
    """One key pair for the run: RSA generation is the slow part of this file."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, certificate.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def configured(monkeypatch, signing):
    """Environment and the certificate fetcher, but no database connection.

    The connection belongs to the app's own lifespan: a second one taken here
    is disposed the moment the first TestClient exits, and every read after
    that finds session_factory set to None.
    """
    key, pem = signing
    monkeypatch.setenv("EMAIL_BACKEND", "ses")
    monkeypatch.setenv("MAIL_FROM", "potocolom@example.com")
    monkeypatch.setenv("SES_REGION", "eu-west-1")
    monkeypatch.setenv("SES_FEEDBACK_TOPIC_ARN", TOPIC)
    get_settings.cache_clear()

    fetched: list[str] = []

    async def certificate(url: str) -> bytes:
        fetched.append(url)
        return pem

    monkeypatch.setattr(ses_feedback, "_fetch_certificate", certificate)
    ses_feedback._certificates.clear()
    try:
        yield key, fetched
    finally:
        get_settings.cache_clear()


async def _clear() -> None:
    async with db.session_factory() as session:
        await session.execute(text("DELETE FROM suppressed_addresses"))
        await session.commit()


@contextmanager
def _api():
    """One client, one lifespan, one engine, for the whole of a test."""
    with TestClient(app) as client:
        client.portal.call(_clear)
        try:
            yield client
        finally:
            client.portal.call(_clear)


def _signed(key, message: dict, sign: bool = True) -> dict:
    """An SNS envelope, signed the way SNS signs one."""
    canonical = "".join(
        f"{field}\n{message[field]}\n"
        for field in ses_feedback.SIGNED_FIELDS[message["Type"]]
        if field in message
    )
    signature = key.sign(canonical.encode(), padding.PKCS1v15(), hashes.SHA256())
    body = dict(message)
    body["SignatureVersion"] = "2"
    body["SigningCertURL"] = CERT_URL
    body["Signature"] = base64.b64encode(
        signature if sign else b"not the signature"
    ).decode()
    return body


def _notification(payload: dict, topic: str = TOPIC) -> dict:
    return {
        "Type": "Notification",
        "MessageId": "11111111-2222-3333-4444-555555555555",
        "TopicArn": topic,
        "Message": json.dumps(payload),
        "Timestamp": "2026-08-25T12:00:00.000Z",
    }


def _bounce(bounce_type: str, address: str = BOUNCED) -> dict:
    return {
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": bounce_type,
            "bouncedRecipients": [{"emailAddress": address}],
        },
    }


def _complaint(address: str = BOUNCED) -> dict:
    return {
        "notificationType": "Complaint",
        "complaint": {"complainedRecipients": [{"emailAddress": address}]},
    }


async def _suppressed() -> list[SuppressedAddress]:
    async with db.session_factory() as session:
        return list((await session.execute(select(SuppressedAddress))).scalars().all())


@pytest.mark.db
def test_an_install_that_sends_no_ses_mail_has_no_feedback_route(configured, monkeypatch):
    """A self-hosted install could never hold a subscription, so it offers
    nothing here to probe."""
    monkeypatch.setenv("EMAIL_BACKEND", "none")
    get_settings.cache_clear()
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json={"Type": "Notification"}
                           ).status_code == 404


@pytest.mark.db
def test_a_notification_nobody_signed_is_refused(configured):
    key, _ = configured
    body = _signed(key, _notification(_bounce("Permanent")), sign=False)
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 403
        assert client.portal.call(_suppressed) == []


@pytest.mark.db
def test_a_certificate_from_anywhere_but_sns_is_never_fetched(configured):
    """The URL in the message decides where the key comes from, so an
    unchecked one is a request this API makes on a stranger's behalf, from
    inside the private subnet, before any signature has been checked."""
    key, fetched = configured
    body = _signed(key, _notification(_bounce("Permanent")))
    body["SigningCertURL"] = "https://sns.eu-west-1.amazonaws.com.evil.example/cert.pem"
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 403
        assert fetched == []
        assert client.portal.call(_suppressed) == []


@pytest.mark.db
def test_a_genuinely_signed_message_from_another_topic_is_refused(configured):
    """Anybody can own an SNS topic, and SNS signs every topic in a region
    with the same key. The topic is what makes the message ours."""
    key, _ = configured
    other = "arn:aws:sns:eu-west-1:999999999999:someone-elses-topic"
    body = _signed(key, _notification(_bounce("Permanent"), topic=other))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 403
        assert client.portal.call(_suppressed) == []


@pytest.mark.db
def test_a_signature_over_a_different_message_is_refused(configured):
    """Signed once, delivered with the payload swapped: the canonical string
    has to cover the Message field or a replayed envelope carries anything."""
    key, _ = configured
    body = _signed(key, _notification(_bounce("Transient")))
    body["Message"] = json.dumps(_bounce("Permanent"))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 403
        assert client.portal.call(_suppressed) == []


@pytest.mark.db
def test_a_permanent_bounce_retires_the_address(configured):
    key, _ = configured
    body = _signed(key, _notification(_bounce("Permanent")))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 204
        rows = client.portal.call(_suppressed)
    assert [(row.email, row.reason) for row in rows] == [("bounced@example.com", "bounce")]


@pytest.mark.db
def test_a_transient_bounce_leaves_the_address_alone(configured):
    """A full mailbox and an out-of-office are transient bounces. Retiring an
    address for one locks a real person out of the reset link they wanted."""
    key, _ = configured
    body = _signed(key, _notification(_bounce("Transient")))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 204
        assert client.portal.call(_suppressed) == []


@pytest.mark.db
def test_a_complaint_retires_the_address(configured):
    key, _ = configured
    body = _signed(key, _notification(_complaint()))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 204
        rows = client.portal.call(_suppressed)
    assert [(row.email, row.reason) for row in rows] == [
        ("bounced@example.com", "complaint")]


@pytest.mark.db
def test_a_replayed_notification_changes_nothing(configured):
    """SNS delivers at least once, so the same bounce arrives twice."""
    key, _ = configured
    body = _signed(key, _notification(_bounce("Permanent")))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 204
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 204
        assert len(client.portal.call(_suppressed)) == 1


@pytest.mark.db
def test_a_subscription_is_confirmed_only_for_our_own_topic(configured, monkeypatch):
    key, _ = configured
    confirmed: list[str] = []

    async def visit(url: str) -> None:
        confirmed.append(url)

    monkeypatch.setattr(ses_feedback, "_visit_subscribe_url", visit)
    subscribe = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
    message = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "99999999-2222-3333-4444-555555555555",
        "TopicArn": TOPIC,
        "Message": "You have chosen to subscribe",
        "SubscribeURL": subscribe,
        "Token": "t",
        "Timestamp": "2026-08-25T12:00:00.000Z",
    }
    stranger = dict(message, TopicArn="arn:aws:sns:eu-west-1:999999999999:theirs")
    with _api() as client:
        assert client.post("/api/v1/mail/feedback",
                           json=_signed(key, message)).status_code == 204
        assert confirmed == [subscribe]
        assert client.post("/api/v1/mail/feedback",
                           json=_signed(key, stranger)).status_code == 403
        assert confirmed == [subscribe]


@pytest.mark.db
def test_feedback_without_a_configured_topic_is_refused(configured, monkeypatch):
    """An unset topic is not a wildcard. Believing every signed message would
    let anybody who can create a topic retire any address on this install."""
    key, _ = configured
    monkeypatch.delenv("SES_FEEDBACK_TOPIC_ARN")
    get_settings.cache_clear()
    body = _signed(key, _notification(_bounce("Permanent")))
    with _api() as client:
        assert client.post("/api/v1/mail/feedback", json=body).status_code == 403
        assert client.portal.call(_suppressed) == []
