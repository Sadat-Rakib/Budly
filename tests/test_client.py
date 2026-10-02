"""Canvas client: pagination, backoff, and auth failure."""

from __future__ import annotations

import httpx
import pytest
import respx

from canvasbuddy.canvas.client import (
    CanvasClient,
    CanvasError,
    RateLimitedError,
    TokenRevokedError,
)
from canvasbuddy.config import Settings

BASE = "https://example.instructure.com/api/v1"


def make_settings(**overrides: object) -> Settings:
    defaults = dict(
        canvas_base_url=BASE,
        canvas_token="test-token",
        database_url="postgresql://u:p@localhost/db",
        max_retries=2,
        rate_limit_sleep_seconds=0.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff is exercised for its control flow, not its wall-clock behaviour."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("canvasbuddy.canvas.client._sleep", _instant)


@respx.mock
async def test_follows_link_header_across_pages() -> None:
    """Each page's next URL must be followed verbatim.

    The third page is addressed by a bookmark cursor rather than a page number, which
    is the case that breaks any implementation that rebuilds the URL from parsed
    parameters instead of using the one Canvas handed back.
    """
    requested: list[str] = []
    pages = {
        None: (
            [{"id": 1}],
            f'<{BASE}/things?page=2&per_page=100>; rel="next"',
        ),
        "2": (
            [{"id": 2}],
            f'<{BASE}/things?page=bookmark:WyJjb3Vyc2UiLDVd&per_page=100>; rel="next"',
        ),
        "bookmark:WyJjb3Vyc2UiLDVd": ([{"id": 3}], None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        body, next_link = pages[request.url.params.get("page")]
        headers = {"Link": next_link} if next_link else {}
        return httpx.Response(200, json=body, headers=headers)

    respx.get(f"{BASE}/things").mock(side_effect=handler)

    async with CanvasClient(make_settings()) as client:
        items = await client.get_paginated("/things")

    assert [i["id"] for i in items] == [1, 2, 3]
    assert "page=bookmark%3AWyJjb3Vyc2UiLDVd" in requested[2] or "bookmark:" in requested[2]


@respx.mock
async def test_falls_back_to_meta_pagination() -> None:
    """A few endpoints paginate through the body instead of the Link header."""
    pages = {
        None: {"things": [{"id": 1}], "meta": {"pagination": {"next": f"{BASE}/things?page=2"}}},
        "2": {"things": [{"id": 2}], "meta": {"pagination": {}}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("page")])

    respx.get(f"{BASE}/things").mock(side_effect=handler)

    async with CanvasClient(make_settings()) as client:
        items = await client.get_paginated("/things")

    assert [i["id"] for i in items] == [1, 2]


@respx.mock
async def test_a_self_referential_next_link_terminates() -> None:
    """A cyclic next link must not spin forever.

    Without a guard this loops indefinitely, burning API quota and hanging the sync
    with no error to show for it.
    """
    respx.get(f"{BASE}/things").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1}],
            headers={"Link": f'<{BASE}/things>; rel="next"'},
        )
    )

    async with CanvasClient(make_settings()) as client:
        items = await client.get_paginated("/things")

    assert items == [{"id": 1}]


@respx.mock
async def test_requests_a_full_page() -> None:
    """Canvas defaults to 10 per page, which silently truncates results."""
    route = respx.get(f"{BASE}/things").mock(return_value=httpx.Response(200, json=[]))

    async with CanvasClient(make_settings()) as client:
        await client.get_paginated("/things")

    assert route.calls.last.request.url.params["per_page"] == "100"


@respx.mock
async def test_401_raises_token_revoked() -> None:
    respx.get(f"{BASE}/users/self").mock(return_value=httpx.Response(401, json={}))

    async with CanvasClient(make_settings()) as client:
        with pytest.raises(TokenRevokedError):
            await client.get_self()


@respx.mock
async def test_low_quota_triggers_a_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("canvasbuddy.canvas.client._sleep", _record)
    respx.get(f"{BASE}/things").mock(
        return_value=httpx.Response(200, json=[], headers={"X-Rate-Limit-Remaining": "40.0"})
    )

    settings = make_settings(rate_limit_floor=100.0, rate_limit_sleep_seconds=60.0)
    async with CanvasClient(settings) as client:
        await client.get_paginated("/things")
        assert client.rate_limit_remaining == 40.0

    assert slept == [60.0]


@respx.mock
async def test_healthy_quota_does_not_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("canvasbuddy.canvas.client._sleep", _record)
    respx.get(f"{BASE}/things").mock(
        return_value=httpx.Response(200, json=[], headers={"X-Rate-Limit-Remaining": "699.0"})
    )

    async with CanvasClient(make_settings()) as client:
        await client.get_paginated("/things")

    assert slept == []


@respx.mock
async def test_429_retries_then_gives_up() -> None:
    route = respx.get(f"{BASE}/things").mock(
        return_value=httpx.Response(429, text="Rate Limit Exceeded")
    )

    async with CanvasClient(make_settings(max_retries=2)) as client:
        with pytest.raises(RateLimitedError):
            await client.get_paginated("/things")

    assert route.call_count == 3  # initial attempt plus two retries


@respx.mock
async def test_403_that_is_not_a_rate_limit_is_not_retried() -> None:
    """A permission error is permanent; retrying it just wastes quota."""
    route = respx.get(f"{BASE}/things").mock(
        return_value=httpx.Response(403, text="user not authorized to perform that action")
    )

    async with CanvasClient(make_settings()) as client:
        with pytest.raises(CanvasError):
            await client.get_paginated("/things")

    assert route.call_count == 1


@respx.mock
async def test_recovers_from_a_transient_500() -> None:
    route = respx.get(f"{BASE}/things")
    route.side_effect = [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json=[{"id": 1}]),
    ]

    async with CanvasClient(make_settings()) as client:
        items = await client.get_paginated("/things")

    assert items == [{"id": 1}]


@respx.mock
async def test_restricted_files_tab_is_not_an_error() -> None:
    """Students frequently cannot see the files tab; that is normal, not a failure."""
    respx.get(f"{BASE}/courses/1/files").mock(
        return_value=httpx.Response(403, text="user not authorized")
    )

    async with CanvasClient(make_settings()) as client:
        assert await client.get_files(1) == []
