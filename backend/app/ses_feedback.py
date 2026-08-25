"""What SES says about an address after the send succeeded.

A send that returns 200 and bounces an hour later tells the API nothing: the
verdict arrives separately, as an SNS notification to this endpoint. Taking it
here rather than polling is the only shape SES offers.

Suppression is a denial of mail to a real person, so a message is believed only
when SNS signed it and the topic is the one this install configured. Anybody
can create an SNS topic, and every topic in a region is signed with the same
key, so the signature alone says "some AWS customer sent this" and not "our
provider said this". The topic is what makes it ours, and the topic's own
access policy, which admits only SES, is what makes the topic worth anything
(docs/aws-setup.md).
"""

import base64
import json
import logging
import re
from urllib.parse import urlsplit

import httpx
from anyio import to_thread
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from app import mail
from app.settings import get_settings

logger = logging.getLogger("potocolom.mail")

router = APIRouter()

REFUSED = HTTPException(status_code=403, detail="not a message from the configured topic")
TOO_LARGE = HTTPException(status_code=413, detail="notification too large")

# The order AWS signs in. A field absent from the message is absent from the
# string, which is how an optional Subject is handled.
SIGNED_FIELDS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
    ),
    "UnsubscribeConfirmation": (
        "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
    ),
}

# The China partition answers on amazonaws.com.cn, so a `.com`-only pattern
# refuses every genuine notification there rather than failing visibly.
_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
# AWS publishes the signing certificate under this one name shape. Pinning the
# path as well as the host leaves nowhere else on that host to aim the fetch,
# and the fetch is a request this API makes unauthenticated, from inside the
# private subnet, before anything in the message has been verified.
_SNS_CERTIFICATE_PATH = re.compile(r"^/SimpleNotificationService-[A-Za-z0-9]+\.pem$")

# SNS caps a message at 256 KiB and the envelope adds little. The body is read
# before any signature exists to check it, so this ceiling is what stops an
# unauthenticated caller deciding how much memory the request costs.
MAX_BODY_BYTES = 512 * 1024

# One entry per certificate rotation, keyed by URL, because AWS mints a new
# filename when it rotates. Cleared wholesale rather than evicted one at a
# time: the working set is a single entry, and anything cleverer would be an
# eviction policy for a dictionary that holds one thing.
_certificates: dict[str, bytes] = {}
_CERTIFICATE_LIMIT = 8

FETCH_TIMEOUT = 5.0


# Every field this module reads, whether to verify or to act on. Checked
# rather than trusted because a JSON body decides these types: an array where
# a string belongs would otherwise reach `urlsplit`, `b64decode` or a dict
# lookup and raise, so an unauthenticated caller would be choosing between a
# 403 and a 500. Only these, not every field, because SNS sends
# MessageAttributes as an object and refusing that would refuse real
# notifications.
_STRING_FIELDS = (
    "Type", "MessageId", "TopicArn", "Message", "Subject", "Timestamp",
    "SignatureVersion", "Signature", "SigningCertURL", "SubscribeURL", "Token",
)


def _well_typed(message: dict) -> bool:
    return all(isinstance(message[field], str)
               for field in _STRING_FIELDS if field in message)


def _certificate_url_is_sns(url: str) -> bool:
    split = urlsplit(url)
    return (split.scheme == "https"
            and _SNS_HOST.match(split.hostname or "") is not None
            and _SNS_CERTIFICATE_PATH.match(split.path) is not None
            and not split.query)


async def _fetch_certificate(url: str) -> bytes:
    # Redirects are not followed, and that is stated rather than inherited: a
    # 302 off an SNS host would undo the check that chose this URL.
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _confirm(topic: str, token: str) -> None:
    import boto3

    boto3.client("sns", region_name=get_settings().ses_region).confirm_subscription(
        TopicArn=topic, Token=token,
        # Without this, whoever holds an UnsubscribeURL can end the
        # subscription with no AWS credential, and that URL is in every
        # notification SNS delivers here.
        AuthenticateOnUnsubscribe="true",
    )


async def _confirm_subscription(topic: str, token: str) -> None:
    """Confirmed by calling SNS with this task's own credentials, rather than
    by fetching the SubscribeURL the message carries.

    That URL holds the token in its query string, and httpx logs a request
    line carrying the whole URL at INFO, which `logs.py` puts on the root
    logger and the cloud ships to CloudWatch. It is the shape decisions.md
    already records for ECS Exec: a one-use capability copied into a log for
    the retention period. Calling the API instead also means the confirmation
    is authenticated, and the topic is ours rather than the sender's. Needs
    `sns:ConfirmSubscription` on the task role.
    """
    await to_thread.run_sync(_confirm, topic, token)


async def _public_key(url: str):
    cached = _certificates.get(url)
    if cached is None:
        cached = await _fetch_certificate(url)
        # Counted after the await, not before: two concurrent misses would
        # both read the old size and both decide there was room.
        if len(_certificates) >= _CERTIFICATE_LIMIT:
            _certificates.clear()
        _certificates[url] = cached
    return load_pem_x509_certificate(cached).public_key()


def _canonical(message: dict) -> bytes:
    return "".join(
        f"{field}\n{message[field]}\n"
        for field in SIGNED_FIELDS[message["Type"]]
        if field in message
    ).encode()


async def _verified(message: dict) -> None:
    """Raises unless SNS signed this exact message with the regional key.

    Only SignatureVersion 2 is accepted. Version 1 signs with SHA-1, and a
    topic can be told to use 2, so accepting both would keep the weaker one
    alive for the sake of a setting the AWS guide tells the operator to make.
    """
    if message.get("SignatureVersion") != "2":
        raise REFUSED
    url = message.get("SigningCertURL", "")
    if not _certificate_url_is_sns(url):
        # Before the fetch, deliberately: the check is what stops the fetch.
        logger.warning("refused an SNS message naming a certificate outside SNS")
        raise REFUSED
    try:
        signature = base64.b64decode(message["Signature"], validate=True)
        key = await _public_key(url)
        key.verify(signature, _canonical(message), padding.PKCS1v15(), hashes.SHA256())
    except (KeyError, ValueError, InvalidSignature, httpx.HTTPError) as refused:
        logger.warning("refused an SNS message that did not verify")
        raise REFUSED from refused


def _recipients(entries: object) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [entry["emailAddress"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("emailAddress"), str)]


def _addresses(payload: dict) -> tuple[str, list[str]]:
    """The addresses this notification retires, and why.

    Identity notifications name the verdict `notificationType` and
    configuration-set event destinations name it `eventType`, for the same
    verdict in the same shape. Reading both means an install wired the second
    way is not silently ignored while its addresses keep bouncing.

    A transient bounce is a full mailbox or a server having a bad afternoon.
    Retiring an address for one would lock a real person out of the reset link
    they are waiting for, so only a permanent verdict counts.
    """
    kind = payload.get("notificationType") or payload.get("eventType")
    if kind == "Complaint":
        complaint = payload.get("complaint")
        entries = complaint.get("complainedRecipients") if isinstance(complaint, dict) else None
        return "complaint", _recipients(entries)
    if kind == "Bounce":
        bounce = payload.get("bounce")
        if not isinstance(bounce, dict) or bounce.get("bounceType") != "Permanent":
            return "bounce", []
        return "bounce", _recipients(bounce.get("bouncedRecipients"))
    return "", []


async def _envelope(request: Request) -> dict:
    """Read with a ceiling, then parse. None of this is authenticated yet."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise TOO_LARGE
        body.extend(chunk)
    try:
        message = json.loads(body)
    except ValueError as malformed:
        raise REFUSED from malformed
    if not isinstance(message, dict) or not _well_typed(message):
        raise REFUSED
    return message


@router.post("/api/v1/mail/feedback", status_code=204)
async def feedback(request: Request) -> Response:
    """SES bounce and complaint notifications, delivered by SNS.

    The endpoint carries no account credential and needs none: the signature
    and the topic are the credential.
    """
    settings = get_settings()
    if settings.email_backend != "ses":
        # An install that sends no SES mail can hold no subscription, so it
        # offers nothing here to probe.
        raise HTTPException(status_code=404, detail="Not Found")
    topic = settings.ses_feedback_topic_arn
    if not topic:
        # An unset topic is not a wildcard. Believing every signed message
        # would let anybody who can create a topic retire any address here.
        logger.warning("refused SNS feedback: SES_FEEDBACK_TOPIC_ARN is unset")
        raise REFUSED

    message = await _envelope(request)
    if message.get("Type") not in SIGNED_FIELDS or message.get("TopicArn") != topic:
        raise REFUSED
    await _verified(message)

    if message["Type"] == "SubscriptionConfirmation":
        await _confirm_subscription(topic, message.get("Token", ""))
        return Response(status_code=204)
    if message["Type"] == "UnsubscribeConfirmation":
        # Nothing to do and nothing to refuse: the subscription is already gone
        # by the time this arrives. Recorded so the silence is explained.
        logger.warning("the SES feedback subscription was removed")
        return Response(status_code=204)

    try:
        payload = json.loads(message["Message"])
    except (KeyError, TypeError, ValueError):
        raise REFUSED from None
    reason, addresses = _addresses(payload if isinstance(payload, dict) else {})
    for address in addresses:
        await mail.suppress(address, reason)
    return Response(status_code=204)
