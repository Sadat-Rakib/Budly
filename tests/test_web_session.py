"""Signed-cookie sessions and the login gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from canvasbuddy.config import Settings
from canvasbuddy.web import session as web_session
from canvasbuddy.web.session import (
    LoginGate,
    SessionError,
    check_password,
    clear_cookie_header,
    cookie_header,
    mint_session,
    verify_session,
)


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "dashboard_password": "open sesame",
        "app_secret": "signing-secret",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class TestMintAndVerify:
    def test_roundtrip(self) -> None:
        settings = make_settings()
        token = mint_session(settings)
        assert verify_session(settings, token)

    def test_tampered_payload_is_rejected(self) -> None:
        settings = make_settings()
        token = mint_session(settings)
        payload, _, signature = token.partition(".")
        forged = "eyJleHAiOiIyMDk5LTAxLTAxVDAwOjAwOjAwKzAwOjAwIn0." + signature
        assert forged != f"{payload}.{signature}"
        assert not verify_session(settings, forged)

    def test_wrong_secret_is_rejected(self) -> None:
        token = mint_session(make_settings())
        assert not verify_session(make_settings(app_secret="other"), token)

    def test_expired_is_rejected(self) -> None:
        settings = make_settings()
        past = datetime.now(UTC) - timedelta(days=web_session.SESSION_DAYS + 1)
        token = mint_session(settings, now=past)
        assert not verify_session(settings, token)

    def test_garbage_is_rejected(self) -> None:
        settings = make_settings()
        assert not verify_session(settings, None)
        assert not verify_session(settings, "")
        assert not verify_session(settings, "not-a-token")
        assert not verify_session(settings, "a.b")

    def test_mint_without_secret_raises(self) -> None:
        with pytest.raises(SessionError):
            mint_session(make_settings(app_secret=None, cron_secret=None))


class TestSecretFallback:
    def test_cron_secret_is_a_fallback_signing_key(self) -> None:
        settings = make_settings(app_secret=None, cron_secret="cron-secret")
        assert verify_session(settings, mint_session(settings))

    def test_dashboard_login_enabled_requires_both(self) -> None:
        assert make_settings().dashboard_login_enabled
        assert not make_settings(app_secret=None, cron_secret=None).dashboard_login_enabled
        assert not make_settings(dashboard_password=None).dashboard_login_enabled


class TestPassword:
    def test_correct_password(self) -> None:
        assert check_password(make_settings(), "open sesame")

    def test_wrong_password(self) -> None:
        assert not check_password(make_settings(), "open  sesame")

    def test_nothing_configured(self) -> None:
        assert not check_password(make_settings(dashboard_password=None), "open sesame")
        assert not check_password(make_settings(), None)


class TestCookieHeaders:
    def test_set_cookie_is_httponly(self) -> None:
        header = cookie_header("tok", secure=True)
        assert header.startswith("sb_session=tok;")
        assert "HttpOnly" in header
        assert "SameSite=Lax" in header
        assert "Secure" in header

    def test_clear_cookie_expires(self) -> None:
        assert "Max-Age=0" in clear_cookie_header()


class TestLoginGate:
    def test_blocks_after_max_attempts(self) -> None:
        gate = LoginGate(max_attempts=3, window=300)
        now = 1000.0
        for _ in range(3):
            assert gate.allows("1.2.3.4", now=now)
            gate.record_failure("1.2.3.4", now=now)
        assert not gate.allows("1.2.3.4", now=now)

    def test_window_expiry_restores_access(self) -> None:
        gate = LoginGate(max_attempts=2, window=300)
        gate.record_failure("1.2.3.4", now=0)
        gate.record_failure("1.2.3.4", now=10)
        assert not gate.allows("1.2.3.4", now=200)
        assert gate.allows("1.2.3.4", now=400)

    def test_ips_are_independent(self) -> None:
        gate = LoginGate(max_attempts=1, window=300)
        gate.record_failure("1.1.1.1", now=0)
        assert not gate.allows("1.1.1.1", now=1)
        assert gate.allows("2.2.2.2", now=1)
