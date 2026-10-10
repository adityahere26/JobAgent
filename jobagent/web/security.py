"""Local-only guard rails for the dashboard.

- The server binds to 127.0.0.1 only.
- Every request must carry a Host header naming this server (blocks DNS rebinding).
- Every POST must come from this origin and carry the per-process CSRF token.
- Every response forbids loading anything from anywhere else (Content-Security-Policy).
"""

import hmac
import secrets
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import PlainTextResponse, Response

from jobagent.config import ConfigError

BIND_HOST = "127.0.0.1"
MAX_FORM_BYTES = 512 * 1024  # a long job description plus a model reply fits easily
SAFE_METHODS = {"GET", "HEAD"}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def check_bind_host(host: str) -> None:
    """Refuse to listen anywhere but the loopback address. Job data stays on this machine."""
    if host != BIND_HOST:
        raise ConfigError(f"The dashboard only listens on {BIND_HOST}; refusing host {host!r}.")


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


class GuardMiddleware(BaseHTTPMiddleware):
    """Host check on every request, Origin check on every POST, security headers on all."""

    def __init__(self, app, port: int) -> None:
        super().__init__(app)
        self.hosts = allowed_hosts(port)
        self.origins = {f"http://{h}" for h in self.hosts}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.get("host") not in self.hosts:
            response: Response = PlainTextResponse("Unknown host.", status_code=400)
        elif request.method not in SAFE_METHODS and (
            request.headers.get("origin") not in self.origins
        ):
            response = PlainTextResponse("Cross-origin request refused.", status_code=403)
        else:
            response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers[name] = value
        return response


class FormFields(dict[str, str]):
    """First value of each field; getlist() gives every value (e.g. several ticked checkboxes)."""

    def __init__(self, lists: dict[str, list[str]]) -> None:
        super().__init__({k: v[0] for k, v in lists.items()})
        self._lists = lists

    def getlist(self, key: str) -> list[str]:
        return list(self._lists.get(key, []))


async def csrf_form(request: Request) -> FormFields:
    """Parse a urlencoded POST body and check its CSRF token. Every POST route depends on this.

    Returns the fields (first value of each; getlist() for repeated ones). The token may come
    in the form or, for scripts, in the X-CSRF-Token header.
    """
    ctype = request.headers.get("content-type", "").split(";")[0].strip()
    if ctype != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Forms only.")
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(413, "That is too much text.")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    fields = FormFields(parsed)
    sent = fields.pop("csrf", "") or request.headers.get("x-csrf-token", "")
    expected: str = request.app.state.csrf
    if not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(403, "The form expired. Reload the page and try again.")
    return fields
