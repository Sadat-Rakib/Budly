"""Voice, mute, contacts, and calendar export."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, time

import httpx
import pytest
import respx

from canvasbuddy.agent.contacts import _IGNORE, _line_around, _role_near, mailto_link
from canvasbuddy.agent.transcribe import TranscriptionError, transcribe
from canvasbuddy.config import Settings
from canvasbuddy.digest.calendar import _escape, _fold, _stamp
from canvasbuddy.llm.openrouter import OpenRouterClient

OR = "https://openrouter.ai/api/v1"


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.university.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "openrouter_api_key": "sk-or-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def said(text: str) -> dict:
    return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}


class TestTranscription:
    @respx.mock
    async def test_ogg_is_sent_straight_through(self) -> None:
        """Telegram sends OGG/Opus and OpenRouter accepts it, so there is no conversion
        step and no ffmpeg in the container."""
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=said("what's due this week"))

        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)

        settings = make_settings()
        async with OpenRouterClient(settings) as llm:
            text = await transcribe(settings, llm, b"OggS-fake-audio", audio_format="ogg")

        assert text == "what's due this week"
        part = seen[0]["messages"][0]["content"][1]
        assert part["type"] == "input_audio"
        assert part["input_audio"]["format"] == "ogg"
        assert base64.b64decode(part["input_audio"]["data"]) == b"OggS-fake-audio"

    @respx.mock
    async def test_transcription_uses_the_audio_model_not_the_chat_model(self) -> None:
        """The chat model need not accept audio; a cheap audio-capable one is used."""
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=said("hello"))

        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)

        settings = make_settings(transcription_model="google/gemini-2.5-flash-lite")
        async with OpenRouterClient(settings) as llm:
            await transcribe(settings, llm, b"audio")

        assert seen[0]["model"] == "google/gemini-2.5-flash-lite"
        assert "tools" not in seen[0]

    @respx.mock
    async def test_unintelligible_audio_is_an_error_not_a_question(self) -> None:
        """Otherwise the sentinel gets forwarded to the agent as if it were a question."""
        respx.post(f"{OR}/chat/completions").mock(
            return_value=httpx.Response(200, json=said("[unintelligible]"))
        )
        settings = make_settings()
        async with OpenRouterClient(settings) as llm:
            with pytest.raises(TranscriptionError):
                await transcribe(settings, llm, b"audio")

    async def test_empty_audio_is_rejected_without_a_call(self) -> None:
        settings = make_settings()
        async with OpenRouterClient(settings) as llm:
            with pytest.raises(TranscriptionError):
                await transcribe(settings, llm, b"")

    async def test_oversized_audio_is_rejected(self) -> None:
        from canvasbuddy.agent.transcribe import MAX_AUDIO_BYTES

        settings = make_settings()
        async with OpenRouterClient(settings) as llm:
            with pytest.raises(TranscriptionError):
                await transcribe(settings, llm, b"x" * (MAX_AUDIO_BYTES + 1))


class TestCalendarEncoding:
    def test_special_characters_are_escaped(self) -> None:
        """RFC 5545 treats comma, semicolon and backslash as separators."""
        assert _escape("Quiz 1, part 2") == "Quiz 1\\, part 2"
        assert _escape("a;b") == "a\\;b"
        assert _escape("C:\\path") == "C:\\\\path"
        assert _escape("line\nbreak") == "line\\nbreak"

    def test_short_lines_are_untouched(self) -> None:
        assert _fold("SUMMARY:Quiz 1") == "SUMMARY:Quiz 1"

    def test_long_lines_are_folded_with_a_leading_space(self) -> None:
        """Unfolded long lines are the usual reason a calendar file is rejected."""
        folded = _fold("DESCRIPTION:" + "x" * 200)
        assert "\r\n " in folded
        for piece in folded.split("\r\n"):
            assert len(piece.encode()) <= 76

    def test_timestamps_are_utc_with_a_z(self) -> None:
        stamp = _stamp(datetime(2026, 11, 16, 4, 59, 59, tzinfo=UTC))
        assert stamp == "20261116T045959Z"

    def test_a_local_time_is_converted_to_utc(self) -> None:
        from zoneinfo import ZoneInfo

        local = datetime(2026, 11, 15, 23, 59, 59, tzinfo=ZoneInfo("America/Toronto"))
        assert _stamp(local) == "20261116T045959Z"


class TestContactHarvesting:
    def test_publisher_and_noreply_addresses_are_ignored(self) -> None:
        """A syllabus is full of addresses that are not a person to email."""
        for junk in (
            "no-reply@university.edu",
            "support@wiley.com",
            "help@pearson.com",
            "info@example.com",
        ):
            assert _IGNORE.search(junk)

    def test_a_real_address_is_kept(self) -> None:
        assert not _IGNORE.search("j.kong@university.edu")

    def test_role_is_read_from_surrounding_words(self) -> None:
        text = "Course Instructor: Professor Kong, j.kong@university.edu, office IC 380"
        assert _role_near(text, text.index("j.kong")) == "instructor"

    def test_ta_role_is_detected(self) -> None:
        text = "Teaching Assistant: Sam Lee (sam.lee@mail.university.edu)"
        assert _role_near(text, text.index("sam.lee")) == "ta"

    def test_the_source_line_is_captured(self) -> None:
        text = "header\nInstructor: Kong, j.kong@university.edu\nfooter"
        line = _line_around(text, text.index("j.kong"))
        assert "Instructor: Kong" in line
        assert "header" not in line


class TestMailto:
    def test_subject_and_body_are_encoded(self) -> None:
        link = mailto_link("prof@university.edu", "MGAB03: Term test", "Hi,\n\nQuick question.")
        assert link.startswith("mailto:prof%40university.edu?")
        assert "subject=MGAB03%3A%20Term%20test" in link
        assert "%0A" in link

    def test_ampersands_do_not_break_the_url(self) -> None:
        link = mailto_link("a@b.ca", "Q&A", "one & two")
        assert "%26" in link


class TestMuteParsing:
    """/mute accepts what a person types, and refuses what it cannot read."""

    import re as _re

    PATTERN = _re.compile(r"(\d+)\s*([dhm])")

    @pytest.mark.parametrize(
        ("raw", "amount", "unit"),
        [("3d", 3, "d"), ("12h", 12, "h"), ("30m", 30, "m"), ("1 d", 1, "d")],
    )
    def test_accepted_forms(self, raw: str, amount: int, unit: str) -> None:
        match = self.PATTERN.fullmatch(raw)
        assert match is not None
        assert (int(match.group(1)), match.group(2)) == (amount, unit)

    @pytest.mark.parametrize("raw", ["forever", "3 weeks", "", "d3"])
    def test_rejected_forms(self, raw: str) -> None:
        assert self.PATTERN.fullmatch(raw) is None


def test_manual_item_bare_date_means_end_of_day() -> None:
    """ "due friday" means friday night, not friday at midnight-past."""
    parsed = datetime.fromisoformat("2026-10-16")
    assert (parsed.hour, parsed.minute) == (0, 0)
    adjusted = parsed.replace(hour=23, minute=59)
    assert adjusted.time() == time(23, 59)
    assert adjusted.date() == date(2026, 10, 16)
