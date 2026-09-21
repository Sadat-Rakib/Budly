"""Settings that people fill in by hand, usually by pasting into Railway."""

from __future__ import annotations

import pytest

from canvasbuddy.config import Settings


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        # A developer .env in the repo root must not leak into assertions: with
        # env_ignore_empty, a monkeypatched "" is skipped and the file's value wins.
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class TestCanvasAddress:
    @pytest.mark.parametrize(
        "pasted",
        [
            "https://myschool.instructure.com",
            "https://myschool.instructure.com/",
            "https://myschool.instructure.com/api/v1",
            "https://myschool.instructure.com/courses/123/assignments",
            "myschool.instructure.com",
            "  https://myschool.instructure.com  ",
        ],
    )
    def test_any_browser_address_becomes_the_api_root(self, pasted: str) -> None:
        settings = make_settings(canvas_base_url=pasted)
        assert settings.canvas_base_url == "https://myschool.instructure.com/api/v1"


class TestBlankValues:
    def test_a_blank_optional_key_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pasting .env.example with OPENROUTER_API_KEY left empty must not switch chat
        on with an empty key."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        assert not make_settings().openrouter_configured

    def test_a_blank_name_falls_back_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USER_NAME", "")
        assert make_settings().user_name is None
