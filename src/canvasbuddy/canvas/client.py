"""Async Canvas LMS REST client.

Design notes that differ from the obvious implementation:

**Sequential, never concurrent.** Canvas throttles with a leaky bucket sized for
*concurrency*, not volume: the bucket starts near 700 and refills at roughly 10 units
per second, and parallel requests incur a pre-flight penalty that is only refunded on
completion. Instructure's own guidance is that a client making no more than one
simultaneous request is unlikely to ever be throttled. Fanning out with a semaphore is
therefore what *creates* the rate-limit problem, so a full sync pass (~20 calls) runs
one request at a time and never comes close to the limit.

**Pagination has two shapes.** Most endpoints advertise the next page in an RFC 5988
``Link`` header, which httpx parses for us. A few return a JSON object with a
``meta.pagination.next`` URL instead. Either way the URL is *opaque* -- it may carry a
bookmark cursor (``page=bookmark:...``) rather than a page number, so it must be
followed verbatim and never reconstructed from parameters.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime
from typing import Any

import httpx

from canvasbuddy.config import Settings

log = logging.getLogger(__name__)

#: Canvas returns 403 (older deployments) or 429 (newer) when the bucket empties.
_RATE_LIMIT_STATUSES = {403, 429}
_RATE_LIMIT_MARKER = "rate limit exceeded"
_MAX_BACKOFF_SECONDS = 900.0  # 15 minutes
#: A sane ceiling on pages. Nothing a student is enrolled in comes close.
_MAX_PAGES = 200


async def _sleep(seconds: float) -> None:
    """Indirection around ``asyncio.sleep`` so backoff can be made instant in tests.

    Patching ``asyncio.sleep`` directly would replace it for the whole interpreter,
    including the event loop machinery the tests themselves run on.
    """
    await asyncio.sleep(seconds)


class CanvasError(RuntimeError):
    """Any unrecoverable Canvas API failure."""


class TokenRevokedError(CanvasError):
    """The access token was rejected.

    Canvas revokes tokens on password change, and does so silently -- the sync would
    otherwise just stop producing data with no visible cause. This is escalated to a
    Telegram alert rather than logged.
    """


class RateLimitedError(CanvasError):
    """The leaky bucket emptied and backoff has been exhausted."""


class CanvasClient:
    """A thin, sequential Canvas API client.

    Everything the rest of the application needs from Canvas goes through here, so
    swapping the HTTP layer (or the library behind it) stays a one-file change.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = settings.canvas_base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            headers={
                # The Bearer header is preferred over ?access_token=: Canvas strips the
                # token from the URLs it returns in Link headers, so a query-parameter
                # token would have to be re-appended to every next-page URL.
                "Authorization": f"Bearer {settings.canvas_token.get_secret_value()}",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        #: Lowest X-Rate-Limit-Remaining seen this session, for `canvasbuddy doctor`.
        self.rate_limit_remaining: float | None = None

    async def __aenter__(self) -> CanvasClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- low level ----------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request, with retries for transient failures and rate limiting."""
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > self._settings.max_retries:
                    raise CanvasError(f"network failure after {attempt} attempts: {exc}") from exc
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 401:
                raise TokenRevokedError(
                    "Canvas rejected the access token (401). It was most likely revoked "
                    "by a password change, or it expired."
                )

            if self._is_rate_limited(response):
                attempt += 1
                if attempt > self._settings.max_retries:
                    raise RateLimitedError(
                        f"still rate limited after {attempt} attempts; giving up this pass"
                    )
                delay = await self._sleep_backoff(attempt)
                log.warning("Canvas rate limited; backed off %.0fs (attempt %d)", delay, attempt)
                continue

            if response.status_code >= 500:
                attempt += 1
                if attempt > self._settings.max_retries:
                    raise CanvasError(f"Canvas returned {response.status_code} repeatedly")
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                raise CanvasError(
                    f"{method} {url} -> {response.status_code}: {response.text[:300]}"
                )

            await self._observe_quota(response)
            return response

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code not in _RATE_LIMIT_STATUSES:
            return False
        if response.status_code == 429:
            return True
        # A 403 is only a rate limit if it says so; otherwise it is a real permission
        # error and retrying it would be pointless.
        return _RATE_LIMIT_MARKER in response.text.lower()

    async def _sleep_backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at 15 minutes.

        Jitter matters even for a single-client bot: without it, a retry storm after a
        Canvas outage lines every request up on the same boundary.
        """
        delay = min(_MAX_BACKOFF_SECONDS, (2**attempt) * 5.0)
        delay = random.uniform(delay / 2, delay)
        await _sleep(delay)
        return delay

    async def _observe_quota(self, response: httpx.Response) -> None:
        """Track the leaky bucket and pause before it empties."""
        raw = response.headers.get("x-rate-limit-remaining")
        if raw is None:
            return
        try:
            remaining = float(raw)
        except ValueError:
            return

        self.rate_limit_remaining = remaining
        if remaining < self._settings.rate_limit_floor:
            log.warning(
                "Canvas quota low (%.1f remaining); sleeping %.0fs to let the bucket refill",
                remaining,
                self._settings.rate_limit_sleep_seconds,
            )
            await _sleep(self._settings.rate_limit_sleep_seconds)

    # -- pagination ---------------------------------------------------------

    @staticmethod
    def _next_url(response: httpx.Response, payload: Any) -> str | None:
        """Find the next page, treating the URL as opaque.

        Two shapes exist. The Link header is the common one; a handful of endpoints
        instead nest the URL under ``meta.pagination.next``. Returning it verbatim is
        deliberate -- rebuilding it from parsed query parameters loses bookmark cursors.
        """
        link = response.links.get("next")
        if link and link.get("url"):
            return link["url"]
        if isinstance(payload, dict):
            pagination = payload.get("meta", {}).get("pagination", {})
            return pagination.get("next") or None
        return None

    async def get_paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """GET every page of a list endpoint and return the concatenated items.

        Canvas defaults to 10 items per page, which is the single easiest way to
        silently lose data, so ``per_page`` is always set.
        """
        params = dict(params or {})
        params.setdefault("per_page", 100)

        url: str | None = self._url(path)
        first = True
        items: list[dict] = []
        # A malformed or cyclic `next` link would otherwise loop forever, quietly
        # burning quota. Both guards are cheap; the sync is worth more than the
        # last page of a runaway response.
        seen: set[str] = set()
        pages = 0

        while url:
            if url in seen:
                log.warning("Canvas pagination looped back to %s; stopping early", url)
                break
            if pages >= _MAX_PAGES:
                log.warning(
                    "Canvas pagination exceeded %d pages for %s; stopping", _MAX_PAGES, path
                )
                break
            seen.add(url)
            pages += 1

            response = await self._request("GET", url, params=params if first else None)
            first = False
            payload = response.json()

            if isinstance(payload, list):
                items.extend(payload)
            elif isinstance(payload, dict):
                # Endpoints that paginate via `meta` wrap the rows in a single key.
                rows = next(
                    (v for k, v in payload.items() if k != "meta" and isinstance(v, list)),
                    None,
                )
                if rows is None:
                    return [payload]
                items.extend(rows)

            url = self._next_url(response, payload)

        return items

    async def get_one(self, path: str, params: dict[str, Any] | None = None) -> dict:
        response = await self._request("GET", self._url(path), params=params)
        return response.json()

    # -- endpoints ----------------------------------------------------------

    async def get_self(self) -> dict:
        return await self.get_one("/users/self")

    async def get_courses(self) -> list[dict]:
        return await self.get_paginated(
            "/courses",
            {
                "enrollment_state": "active",
                "include[]": ["term", "syllabus_body", "teachers"],
            },
        )

    async def get_enrollments(self, course_id: int) -> list[dict]:
        """This user's enrolments in one course.

        A user can hold several at once -- a lecture section and a tutorial section,
        for instance -- so this is deliberately a list.
        """
        return await self.get_paginated(f"/courses/{course_id}/enrollments", {"user_id": "self"})

    async def get_sections(self, course_id: int) -> list[dict]:
        return await self.get_paginated(f"/courses/{course_id}/sections")

    async def get_assignments(self, course_id: int) -> list[dict]:
        """The canonical assignment list for a course.

        Unlike the planner, this includes assignments with no due date -- which are
        real graded work, not noise -- so it is the source of truth for the store.
        """
        return await self.get_paginated(
            f"/courses/{course_id}/assignments",
            {"include[]": ["submission", "all_dates"], "order_by": "due_at"},
        )

    async def get_planner_items(
        self,
        start_date: date | datetime,
        end_date: date | datetime,
        context_codes: list[str] | None = None,
    ) -> list[dict]:
        """The user's planner feed.

        Valuable because Canvas resolves section overrides server-side here, so the
        dates are the ones that actually apply to this student. Note that anything with
        a null due date is omitted entirely, which is why this is an overlay on
        ``get_assignments`` rather than a replacement for it.
        """
        params: dict[str, Any] = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if context_codes:
            params["context_codes[]"] = context_codes
        return await self.get_paginated("/planner/items", params)

    async def get_announcements(
        self,
        context_codes: list[str],
        start_date: date | datetime,
        end_date: date | datetime,
    ) -> list[dict]:
        """Announcements across every course in one call.

        Requires context codes, which is why courses must be synced first.
        """
        if not context_codes:
            return []
        return await self.get_paginated(
            "/announcements",
            {
                "context_codes[]": context_codes,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )

    async def get_modules(self, course_id: int) -> list[dict]:
        """Course modules with their items.

        The route to a course's documents. ``/courses/:id/files`` is forbidden in most
        courses -- instructors hide the Files tab -- but modules stay readable, and the
        file items inside them resolve to downloadable files anyway.
        """
        return await self.get_paginated(f"/courses/{course_id}/modules", {"include[]": ["items"]})

    async def get_file(self, url: str) -> dict:
        """Resolve a module item's API url to its file object.

        Works even where the file *listing* returns 403: only enumeration is restricted,
        not access to a file the student can already see in a module.
        """
        return await self.get_one(url)

    async def download(self, url: str) -> bytes:
        """Fetch a file's bytes from its (short-lived, verifier-signed) download url."""
        response = await self._request("GET", url)
        return response.content

    async def get_files(self, course_id: int, search_term: str | None = None) -> list[dict]:
        """Course files. Used at P2 to find syllabus PDFs.

        Many courses restrict the files tab to instructors, which surfaces as a 403;
        that is a normal state for a student token, not a failure.
        """
        params = {"search_term": search_term} if search_term else None
        try:
            return await self.get_paginated(f"/courses/{course_id}/files", params)
        except CanvasError as exc:
            if "403" in str(exc):
                log.info("Files tab not visible for course %s; skipping", course_id)
                return []
            raise
