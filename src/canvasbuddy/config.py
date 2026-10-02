"""Application settings, loaded from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _local_timezone_name() -> str:
    """The machine's IANA timezone name, for the default digest schedule.

    tzlocal handles the Windows-to-IANA mapping that the standard library does not.
    If detection somehow fails, the developer's own zone is the fallback rather
    than UTC, because a 7:00 digest silently landing at 1:00 is the kind of bug a
    user never reports and always remembers.
    """
    try:
        import tzlocal

        return tzlocal.get_localzone_name()
    except Exception:  # noqa: BLE001 - detection must never break startup
        return "America/Edmonton"


def _default_database_url() -> str:
    """Local-first storage: a SQLite file in the user's home directory.

    No account, no server, no Docker. Setting DATABASE_URL to a Postgres DSN
    still works for people who already run one (the legacy hosted mode).
    """
    data_dir = Path.home() / ".budly"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{(data_dir / 'budly.db').as_posix()}"


class Settings(BaseSettings):
    """Every knob Budly has. Nothing is hardcoded to a single institution."""

    # env_ignore_empty: people paste the whole .env.example block into their .env with
    # the optional lines left blank. A blank line must mean "not set", not "set to
    # ''" -- otherwise an empty OPENROUTER_API_KEY switches chat on with no key.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # --- Canvas -------------------------------------------------------------
    #: Your school's Canvas address. The plain web address from the browser works
    #: (https://myschool.instructure.com) -- /api/v1 is appended if it's missing.
    #: Optional so Budly can start before it is configured; the dashboard shows
    #: what is missing instead of crashing.
    canvas_base_url: str = ""
    canvas_token: SecretStr | None = None
    canvas_term: str = "2026 Fall"
    #: Serve built-in fixture courses instead of calling Canvas. For local
    #: development and demos without a Canvas account. Never enable this on a
    #: production deployment: syncs then write fixture data, and the dashboard
    #: shows a Demo data badge so it can never pass silently.
    canvas_mock_mode: bool = False
    #: Minutes between automatic background syncs while Budly runs. The local
    #: scheduler owns this loop; manual "Refresh Canvas" always works regardless.
    canvas_sync_interval_minutes: int = Field(default=30, ge=5, le=1440)

    @property
    def canvas_configured(self) -> bool:
        return bool(self.canvas_base_url and self.canvas_token)

    # --- Database -----------------------------------------------------------
    #: Local SQLite by default (see _default_database_url). A Postgres DSN keeps
    #: the legacy hosted mode working for people who already run one.
    database_url: str = Field(default_factory=_default_database_url)
    #: Optional schema isolation for Postgres (e.g. a throwaway demo schema).
    #: Empty means the database default. Ignored on SQLite.
    database_search_path: str = ""

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # --- LLM ----------------------------------------------------------------
    # Routed through OpenRouter, which is OpenAI-compatible, so the same settings work
    # for any model it fronts (and for any other OpenAI-compatible provider by
    # overriding OPENROUTER_BASE_URL). Slugs are pinned exactly rather than using a
    # `~...latest` alias: a floating alias silently changing model underneath an agent
    # loop is a miserable thing to debug.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    #: The primary chat model. Must support tool calling.
    chat_model: str = "anthropic/claude-sonnet-5"
    #: Fallbacks for technical failures only (rate limit, outage, timeout, model
    #: gone). Tried in order; a successful primary never triggers them, and an
    #: invalid API key fails fast instead of burning through the list.
    ai_fallback_model_1: str | None = None
    ai_fallback_model_2: str | None = None
    #: A second provider slot in the chain. Groq is OpenAI-compatible and has a
    #: generous free tier, so a rate-limited OpenRouter day slides to Groq before
    #: Budly gives up. Any OpenAI-compatible provider works the same way.
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    #: Syllabus and exam extraction. Runs a handful of times a term, so accuracy
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
    #: A tz database name, e.g. "America/Edmonton" or "Asia/Dhaka". Defaults to the
    #: machine's own zone; set it explicitly on a server.
    user_timezone: str = Field(default_factory=_local_timezone_name)
    digest_hour: int = Field(default=7, ge=0, le=23)
    nudge_hour: int = Field(default=20, ge=0, le=23)
    # PRD open question 2: running averages daily may cost more anxiety than they
    # return in value. Off by default; the data is stored either way.
    show_grades_in_digest: bool = False

    # --- Notification slots (local scheduler; legacy hosted cron reads these too) --
    #: Empty string disables that role. Validated at startup via slots.parse_slot.
    digest_slot: str = "daily@07:00"
    nudge_slot: str = "daily@20:00"
    review_slot: str = "sat@08:00"
    checkin_slot: str = "sat@15:00"
    notify_grace_minutes: int = Field(default=180, ge=5, le=720)
    review_replaces_daily: bool = True
    review_lookback_days: int = Field(default=7, ge=1, le=30)
    review_lookahead_days: int = Field(default=7, ge=1, le=30)

    # --- Legacy hosted adapters (Telegram webhook on Vercel) -------------------
    #: These protect the optional hosted webhook/cron endpoints. The local app
    #: does not use them: it binds to localhost and needs no secrets of its own.
    webhook_secret: SecretStr | None = None
    cron_secret: SecretStr | None = None
    slack_webhook_url: SecretStr | None = None

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
        link therefore works too. Empty stays empty: Budly can start unconfigured.
        """
        v = v.strip()
        if not v:
            return ""
        if "://" not in v:
            v = "https://" + v
        parts = urlsplit(v)
        return f"{parts.scheme}://{parts.netloc}/api/v1"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        """Normalise whatever the user pasted into a driver SQLAlchemy can use.

        Supabase/Vercel hand out sync URLs; the async engine needs asyncpg, so those
        get rewritten. SQLite URLs (the local default) pass through untouched, as do
        Neon-only params that asyncpg does not understand.
        """
        if v.startswith("sqlite"):
            return v
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
