"""Signed-cookie sessions for the web dashboard.

The dashboard serves one person: whoever holds the Canvas token in the deployment's
environment. A full user table, password hashing and OAuth would all be correct for a
multi-tenant deployment and are all dead weight here, so the model is deliberately
small:

* ``POST /api/login`` compares the posted password to ``DASHBOARD_PASSWORD``.
* On success the browser gets an HttpOnly cookie holding ``payload.signature`` -- a
  JSON body with an expiry, signed with HMAC-SHA256 under ``APP_SECRET`` (or
  ``CRON_SECRET``). Nothing server-side to store, nothing to clean up.
* The cookie is HttpOnly, so no page JavaScript -- not even ours -- can read it, and
  SameSite=Lax so a cross-site post to /api/chat cannot ride along on someone's
  session.

Canvas tokens never appear in any of this. They stay in the server environment and
are only ever read server-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from canvasbuddy.config import Settings

SESSION_COOKIE = "sb_session"
#: Long-lived on purpose: this is one person's own dashboard, and re-typing a password
#: every visit trains them to store it somewhere worse.
SESSION_DAYS = 30
#: Login attempts per IP per instance window. Serverless instances are ephemeral, so
#: this is a speed bump rather than a wall -- but a slow brute force across cold starts
#: is much less convenient than a fast one.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300


class SessionError(ValueError):
    """The cookie is absent, malformed, forged, or expired."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _mac(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()


def mint_session(settings: Settings, *, now: datetime | None = None) -> str:
    """A signed token for a fresh session."""
    secret = settings.session_secret
    if secret is None:
        raise SessionError("no session secret configured")
    expires = (now or datetime.now(UTC)) + timedelta(days=SESSION_DAYS)
    payload = json.dumps({"exp": expires.replace(microsecond=0).isoformat()}).encode()
    # The signature covers the base64 payload text -- exactly the bytes the verifier
    # sees -- rather than the JSON underneath it.
    payload_b64 = _b64(payload)
    return payload_b64 + "." + _b64(_mac(secret.get_secret_value(), payload_b64.encode()))


def verify_session(settings: Settings, token: str | None, *, now: datetime | None = None) -> bool:
    """True when the token carries a valid signature that has not expired.

    Every failure mode -- junk, wrong key, stale -- collapses to False: the caller's
    answer is the same 401 either way.
    """
    secret = settings.session_secret
    if not token or secret is None:
        return False
    payload_b64, _, signature_b64 = token.partition(".")
    if not payload_b64 or not signature_b64:
        return False
    try:
        expected = _mac(secret.get_secret_value(), payload_b64.encode())
        if not hmac.compare_digest(expected, _b64decode(signature_b64)):
            return False
        payload = json.loads(_b64decode(payload_b64))
        expires = datetime.fromisoformat(payload["exp"])
    except (ValueError, KeyError, TypeError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > (now or datetime.now(UTC))


def check_password(settings: Settings, provided: str | None) -> bool:
    secret = settings.dashboard_password
    if secret is None or not provided:
        return False
    return hmac.compare_digest(
        secret.get_secret_value().encode(), provided.encode()
    )


def cookie_header(token: str, *, secure: bool = True) -> str:
    """The Set-Cookie value for a minted session."""
    parts = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_DAYS * 86400}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


class LoginGate:
    """Per-instance failed-login memory.

    Keyed by client IP. An entry is a list of attempt timestamps; anything older than
    the window no longer counts. Instances come and go on serverless platforms, so the
    worst case is a rate limit per cold instance -- still a large practical slowdown
    over unthrottled guessing, at the cost of a dict.
    """

    def __init__(
        self, max_attempts: int = LOGIN_MAX_ATTEMPTS, window: float = LOGIN_WINDOW_SECONDS
    ):
        self._attempts: dict[str, list[float]] = {}
        self._max = max_attempts
        self._window = window

    def allows(self, key: str, *, now: float) -> bool:
        recent = [t for t in self._attempts.get(key, []) if now - t < self._window]
        self._attempts[key] = recent
        return len(recent) < self._max

    def record_failure(self, key: str, *, now: float) -> None:
        self._attempts.setdefault(key, []).append(now)
