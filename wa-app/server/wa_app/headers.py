"""Security headers on every response: the SPA, the API, errors and the 413/401 the middleware answers itself."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# `style-src 'self'` has no 'unsafe-inline': React sets styles through the CSSOM (element.style), which CSP allows,
# and the built app has no style attributes or <style> blocks. The theme bootstrap is a file (theme-init.js), not
# an inline script. If a future build needs inline styles, add 'unsafe-inline' to style-src only.
CSP = (
    "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
    "style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class SecurityHeadersMiddleware:
    """Adds HEADERS to every http response. A header a route already set (the media routes' own stricter
    `Content-Security-Policy: sandbox; default-src 'none'`) is kept as it is."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, wrapped)
