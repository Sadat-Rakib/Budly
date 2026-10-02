"""Application settings, loaded from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every knob StudyBuddy has. Nothing is hardcoded to a single institution."""

    # env_ignore_empty: people paste the whole .env.example block into Railway with the
    # optional lines left blank. A blank line must mean "not set", not "set to ''" --
    # otherwise an empty OPENROUTER_API_KEY switches chat on with no key.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # --- Canvas -------------------------------------------------------------
    #: Your school's Canvas address. The plain web address from the browser works
    #: (https://myschool.instructure.com) -- /api/v1 is appended if it's missing.
    canvas_base_url: str
    canvas_token: SecretStr
    canvas_term: str = "2026 Fall"
    #: Serve built-in fixture courses instead of calling Canvas. For local
    #: development and demos without a Canvas account. Never enable this on a
    #: production deployment: syncs then write fixture data, and the dashboard
    #: shows a Demo data badge so it can never pass silently.
    canvas_mock_mode: bool = False

    # --- Database -----------------------------------------------------------
    database_url: str
    #: Optional schema isolation (e.g. "canvasmock" for a throwaway mock-mode run).
    #: Empty means the database default. Every connection then resolves unqualified
    #: tables inside that schema, leaving the public schema untouched.
    database_search_path: str = ""

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # --- LLM ----------------------------------------------------------------
    # Routed through OpenRouter, which is OpenAI-compatible, so the same settings work
    # for any model it fronts. Slugs are pinned exactly rather than using a `~...latest`
    # alias: a floating alias silently changing model underneath an agent loop is a
    # miserable thing to debug.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    #: The chat agent. Must support tool calling -- checked at startup.
    chat_model: str = "anthropic/claude-sonnet-5"
    #: Syllabus and exam extraction (P2). Runs a handful of times a term, so accuracy
    #: dwarfs cost.
    extraction_model: str = "anthropic/claude-opus-5"
    #: Transcribes voice notes. Must accept audio input -- OpenRouter lists 46 such
    #: models; Telegram sends OGG/Opus, which OpenRouter accepts directly, so no
    #: conversion step and no second provider is needed.
    transcription_model: str = "google/gemini-2.5-flash-lite"
    #: Ceiling on tool-call rounds in one conversation turn, so a confused model cannot
    #: spin indefinitely.
    agent_max_iterations: int = 6
    agent_max_tokens: int = 2048
    #: Turns of conversation replayed to the model. Older turns collapse into a summary.
    agent_history_turns: int = 20

    @property
    def openrouter_configured(self) -> bool:
        return self.openrouter_api_key is not None

    # --- Behaviour ----------------------------------------------------------
    #: What the assistant calls you. Optional.
    user_name: str | None = None
    #: A tz database name, e.g. "America/Edmonton" or "America/Toronto".
    user_timezone: str = "America/Edmonton"
    digest_hour: int = Field(default=7, ge=0, le=23)
    nudge_hour: int = Field(default=20, ge=0, le=23)
    # PRD open question 2: running averages daily may cost more anxiety than they
    # return in value. Off by default; the data is stored either way.
    show_grades_in_digest: bool = False

    # --- Notifier slots (serverless cron) -------------------------------------
    #: Empty string disables that role. Validated at startup via slots.parse_slot.
    digest_slot: str = "daily@07:00"
    nudge_slot: str = "daily@20:00"
    review_slot: str = "sat@08:00"
    checkin_slot: str = "sat@15:00"
    notify_grace_minutes: int = Field(default=180, ge=5, le=720)
    review_replaces_daily: bool = True
    review_lookback_days: int = Field(default=7, ge=1, le=30)
    review_lookahead_days: int = Field(default=7, ge=1, le=30)

    # --- Serverless auth + Slack ----------------------------------------------
    webhook_secret: SecretStr | None = None
    cron_secret: SecretStr | None = None
    slack_webhook_url: SecretStr | None = None

    # --- Web dashboard ---------------------------------------------------------
    #: The password that unlocks the dashboard chat. Left unset, every API behind the
    #: session cookie answers 503 and the mascot explains what to set. This is the whole
    #: auth model for a personal deployment, where the Canvas token already lives in
    #: the environment and there is exactly one person to let in.
    dashboard_password: SecretStr | None = None
    #: Signs the session cookie. Falls back to cron_secret so a deployment that already
    #: generated one secret does not need a second one.
    app_secret: SecretStr | None = None

    @property
    def session_secret(self) -> SecretStr | None:
        return self.app_secret or self.cron_secret

    @property
    def dashboard_login_enabled(self) -> bool:
        return self.dashboard_password is not None and self.session_secret is not None

    @property
    def slack_configured(self) -> bool:
        return self.slack_webhook_url is not None

    # --- HTTP ---------------------------------------------------------------
    # Canvas' leaky bucket starts near 700 and refills ~10/sec. Sequential
    # requests essentially cannot drain it, so this floor is insurance, not a
    # design driver.
    rate_limit_floor: float = 100.0
    rate_limit_sleep_seconds: float = 60.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 4

    @field_validator("canvas_base_url")
    @classmethod
    def _normalise_canvas_url(cls, v: str) -> str:
        """Accept the address students actually see in their browser.

        Asking a non-technical user to append /api/v1 is asking for a typo, so only the
        host is kept and the API path is always rebuilt. A pasted dashboard or course
        link therefore works too.
        """
        v = v.strip()
        if "://" not in v:
            v = "https://" + v
        parts = urlsplit(v)
        return f"{parts.scheme}://{parts.netloc}/api/v1"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        """Supabase/Vercel hand out sync URLs; SQLAlchemy's async engine needs asyncpg.

        Also strips Neon-only params (sslmode/channel_binding) and keeps
        ?ssl=require for asyncpg compatibility.
        """
        # Neon alternative: strip params asyncpg does not understand.
        if "channel_binding=" in v:
            v = v.replace("channel_binding=require", "").replace("channel_binding", "")
        if "sslmode=" in v and "ssl=" not in v:
            v = v.replace("sslmode=require", "ssl=require").replace("sslmode", "ssl")
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v

    @field_validator("digest_slot", "nudge_slot", "review_slot", "checkin_slot")
    @classmethod
    def _validate_slot(cls, v: str, info) -> str:
        """Fail fast on bad slot specs. Empty string disables that role."""
        if not v.strip():
            return ""
        from canvasbuddy.slots import parse_slot

        role_map = {
            "digest_slot": "digest",
            "nudge_slot": "nudge",
            "review_slot": "review",
            "checkin_slot": "checkin",
        }
        parse_slot(role_map[info.field_name], v.strip())
        return v.strip()

    def active_slots(self) -> list:
        """Parsed Slot objects for all enabled roles."""
        from canvasbuddy.slots import parse_slot

        out = []
        for role, spec in (
            ("digest", self.digest_slot),
            ("nudge", self.nudge_slot),
            ("review", self.review_slot),
            ("checkin", self.checkin_slot),
        ):
            if spec.strip():
                out.append(parse_slot(role, spec.strip()))
        return out

    @field_validator("user_timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if the zone is unknown
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.user_timezone)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
