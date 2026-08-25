"""What SES says about an address after the send succeeded.

A send that returns 200 and bounces an hour later tells the API nothing: the
verdict arrives separately, as an SNS notification to this endpoint. Delivering
it here rather than polling is the only shape SES offers.

Suppression is a denial of mail to a real person, so a message is believed only
when SNS signed it and the topic is the one this install configured. Anybody
can create an SNS topic, and every topic in a region is signed with the same
key, so the signature alone says "some AWS customer sent this" and not "our
provider said this". The topic is what makes it ours.
"""

import base64
import json
import logging
import re
from urllib.parse import urlsplit

import httpx
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

_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

# Keyed by URL, and AWS mints a new filename when it rotates, so this grows by
# one per rotation rather than without bound. Cleared wholesale rather than
# evicted: the working set is one entry.
# ponytail: a dict and a ceiling, an LRU if a region ever rotates hourly.
_certificates: dict[str, bytes] = {}
_CERTIFICATE_LIMIT = 8

FETCH_TIMEOUT = 5.0


def _from_sns(url: str) -> bool:
    """Both URLs in the message point somewhere, and this API is the thing that
    would go there. Unchecked, that is a request made from inside the private
    subnet on the sender's behalf, decided by the sender."""
    split = urlsplit(url)
    return split.scheme == "https" and _SNS_HOST.match(split.hostname or "") is not None


async def _fetch_certificate(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _visit_subscribe_url(url: str) -> None:
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        await client.get(url)


async def _public_key(url: str):
    if url not in _certificates:
        if len(_certificates) >= _CERTIFICATE_LIMIT:
            _certificates.clear()
        _certificates[url] = await _fetch_certificate(url)
    return load_pem_x509_certificate(_certificates[url]).public_key()


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
    if not _from_sns(url):
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


def _addresses(payload: dict) -> tuple[str, list[str]]:
    """The addresses this notification retires, and why.

    A transient bounce is a full mailbox or a server having a bad afternoon.
    Retiring an address for one would lock a real person out of the reset link
    they are waiting for, so only a permanent verdict counts.
    """
    kind = payload.get("notificationType")
    if kind == "Bounce":
        bounce = payload.get("bounce") or {}
        if bounce.get("bounceType") != "Permanent":
            return "bounce", []
        recipients = bounce.get("bouncedRecipients") or []
    elif kind == "Complaint":
        recipients = (payload.get("complaint") or {}).get("complainedRecipients") or []
        return "complaint", [
            address for entry in recipients
            if (address := entry.get("emailAddress"))
        ]
    else:
        return "", []
    return "bounce", [
        address for entry in recipients if (address := entry.get("emailAddress"))
    ]


@router.post("/api/v1/mail/feedback", status_code=204)
async def feedback(request: Request) -> Response:
    """SES bounce and complaint notifications, delivered by SNS.

    The endpoint is unauthenticated in the ordinary sense because SNS presents
    no credential; the signature and the topic are the credential.
    """
    settings = get_settings()
    if settings.email_backend != "ses":
        # An install that sends no SES mail can have no subscription, so it
        # offers nothing here to probe.
        raise HTTPException(status_code=404, detail="Not Found")
    topic = settings.ses_feedback_topic_arn
    if not topic:
        # An unset topic is not a wildcard. Believing every signed message
        # would let anybody who can create a topic retire any address here.
        logger.warning("refused SNS feedback: SES_FEEDBACK_TOPIC_ARN is unset")
        raise REFUSED
    try:
        message = await request.json()
    except ValueError as malformed:
        raise REFUSED from malformed
    if not isinstance(message, dict) or message.get("Type") not in SIGNED_FIELDS:
        raise REFUSED
    if message.get("TopicArn") != topic:
        raise REFUSED
    await _verified(message)

    if message["Type"] == "SubscriptionConfirmation":
        subscribe = message.get("SubscribeURL", "")
        if not _from_sns(subscribe):
            raise REFUSED
        await _visit_subscribe_url(subscribe)
        return Response(status_code=204)
    if message["Type"] == "UnsubscribeConfirmation":
        # Nothing to do, and nothing to refuse: the subscription is already
        # gone by the time this arrives. Recorded so the silence is explained.
        logger.warning("the SES feedback subscription was removed")
        return Response(status_code=204)

    try:
        payload = json.loads(message["Message"])
    except (TypeError, ValueError):
        raise REFUSED from None
    reason, addresses = _addresses(payload if isinstance(payload, dict) else {})
    for address in addresses:
        await mail.suppress(address, reason)
    return Response(status_code=204)
