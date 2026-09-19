"""Setup wizard parsing/validation with mocked HTTP."""

import httpx
import pytest
import respx

from canvasbuddy.setup import (
    check_canvas,
    check_slack_webhook,
    check_telegram_bot,
    detect_chat_id,
    generate_secret,
    validate_slot_input,
    validate_timezone,
)


def test_timezone():
    assert validate_timezone("America/Edmonton")
    assert not validate_timezone("Mars/Olympus")


def test_slot_validation():
    assert validate_slot_input("digest", "daily@07:00") == "daily@07:00"
    assert validate_slot_input("review", "") == ""
    with pytest.raises(ValueError):
        validate_slot_input("digest", "someday@07:00")


def test_secret_length():
    s = generate_secret()
    assert len(s) == 48


@respx.mock
async def test_check_canvas():
    respx.get("https://canvas.ualberta.ca/api/v1/users/self").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Mir"})
    )
    respx.get("https://canvas.ualberta.ca/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "term": {"name": "Fall Term 2026"}}])
    )
    info = await check_canvas("https://canvas.ualberta.ca", "tok")
    assert info["user"]["name"] == "Mir"
    assert info["terms"] == ["Fall Term 2026"]


@respx.mock
async def test_telegram():
    token = "123:abc"
    respx.get(f"https://api.telegram.org/bot{token}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "studybot"}})
    )
    me = await check_telegram_bot(token)
    assert me["username"] == "studybot"
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": [{"message": {"chat": {"id": 42}}}]}
        ),
    )
    assert await detect_chat_id(token) == "42"


@respx.mock
async def test_slack():
    route = respx.post("https://hooks.slack.com/services/T000/B000/xxxx").mock(
        return_value=httpx.Response(200, text="ok")
    )
    await check_slack_webhook("https://hooks.slack.com/services/T000/B000/xxxx")
    assert route.called
