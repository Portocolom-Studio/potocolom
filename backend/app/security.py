"""Canonical HTTP security headers for every non-WebSocket response.

The same header map is mirrored in frontend/static/_headers for Cloudflare
Pages. Keep the two byte-for-byte aligned; tests assert the match.
"""

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# HTTP CSP permits inline scripts as a compatibility envelope for any response
# that is not a prerendered SvelteKit document. The generated CSP meta tag in
# each prerendered page supplies the per-build SHA-256 script hash and is the
# effective script-execution restriction for the SPA.
# img-src includes http: so MinIO / S3-compatible endpoints on another origin
# (e.g. http://localhost:9100) remain loadable; HTTPS pages still block HTTP
# images as mixed content.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "frame-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "img-src 'self' https: http:; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'"
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # No includeSubDomains / preload: self-hosted operators may serve
    # unrelated subdomains from the same certificate or host.
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}


def unhandled_exception_response(request: Request, exc: Exception) -> Response:
    """500 body for ServerErrorMiddleware; must carry SECURITY_HEADERS itself.

    Starlette places ServerErrorMiddleware outside user middleware, so responses
    from this handler never pass through SecurityHeadersMiddleware.
    """
    del request, exc  # signature fixed by Starlette ExceptionHandler
    return PlainTextResponse(
        "Internal Server Error",
        status_code=500,
        headers=SECURITY_HEADERS,
    )


class SecurityHeadersMiddleware:
    """Attach SECURITY_HEADERS to every HTTP response; leave WebSockets alone."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
