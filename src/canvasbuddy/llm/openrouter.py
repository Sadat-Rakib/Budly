"""OpenRouter client.

Hand-rolled on ``httpx`` rather than the ``openai`` SDK, and the reason is a hard
constraint rather than a preference: ``openai`` 3.x depends on ``httpx2``, a separate HTTP
stack that cannot coexist cleanly with the ``httpx <0.29`` that python-telegram-bot pins.
Taking the SDK would mean two HTTP clients, two connection pools and two TLS
configurations in one image, to gain typed wrappers around a single endpoint whose
messages we have to hand back as plain dicts anyway.

Three details of OpenRouter's tool calling are easy to get wrong and are handled here:

* ``tools`` must be sent on **every** request, not just the first. OpenRouter revalidates
  the schema each call.
* ``arguments`` arrives as a **string**, and OpenRouter's own documentation is explicit
  that it is unvalidated model output. It is parsed and schema-checked by the caller.
* A model that cannot use tools returns **404** rather than degrading gracefully, and a
  fallback provider may accept ``tools`` and then ignore them. Hence
  :meth:`OpenRouterClient.assert_supports_tools`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from canvasbuddy.config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any unrecoverable failure talking to OpenRouter."""

    #: False for failures where trying another model cannot help (a rejected key,
    #: a malformed request). True for the technical failures fallback exists for.
    retryable = False
    #: Human-readable failure class for logs ("rate limit", "timeout", ...).
    reason = "error"


class ModelUnsupportedError(LLMError):
    """The configured model does not exist, or cannot use tools.

    Retryable because the *model* is the problem: another model in the fallback
    chain may work fine.
    """

    retryable = True
    reason = "model unavailable"


class LLMTransientError(LLMError):
    """Rate limit, outage, temporary server error, or timeout.

    Exactly the classes the fallback chain exists for (PRD §23). An invalid key
    or a malformed request is not here: retrying those with another model just
    burns quota on a failure that will repeat.
    """

    retryable = True
    reason = "transient"


class OpenRouterClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if settings.openrouter_api_key is None:
            raise LLMError("OPENROUTER_API_KEY is not set.")
        self._settings = settings
        self._base_url = settings.openrouter_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            # A tool round-trip through a large model is slow; the connect timeout stays
            # short so a dead network still fails fast.
            timeout=httpx.Timeout(120.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
                # Optional -- they only affect how this app appears in OpenRouter's own
                # activity log, which is worth having when debugging a bad turn.
                "HTTP-Referer": "https://github.com/Sadat-Rakib/StudyBuddy",
                "X-Title": "Budly",
            },
        )

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def assert_supports_tools(self, model: str) -> None:
        """Fail at startup rather than at the user's first message.

        OpenRouter's catalogue moves -- models are added, and capabilities are dropped
        from existing slugs. Without this check the failure surfaces as a bare 404 at the
        exact moment someone asks the bot a question.
        """
        response = await self._client.get(
            f"{self._base_url}/models", params={"supported_parameters": "tools"}
        )
        if response.status_code >= 400:
            # A catalogue lookup failing is not a reason to refuse to start; the model
            # itself may be perfectly fine.
            log.warning("Could not verify model capabilities (%s)", response.status_code)
            return

        slugs = {entry.get("id") for entry in response.json().get("data", [])}
        if slugs and model not in slugs:
            raise ModelUnsupportedError(
                f"{model!r} is not listed by OpenRouter as supporting tool calling. "
                f"Check https://openrouter.ai/models and set CHAT_MODEL to a slug that does."
            )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """One completion, with fallback on technical failures.

        The chain is primary → fallback 1 → fallback 2 (PRD §22). Only technical
        failures move down the chain -- rate limit, outage, timeout, model gone.
        A rejected key or malformed request raises immediately, and a successful
        primary never touches the fallbacks (no wasted quota, PRD §23). When an
        explicit ``model`` was requested, the chain is that model alone.
        """
        if model:
            chain: list[str] = [model]
        else:
            chain = [self._settings.chat_model]
            chain += [
                m
                for m in (
                    self._settings.ai_fallback_model_1,
                    self._settings.ai_fallback_model_2,
                )
                if m
            ]

        for index, candidate in enumerate(chain):
            try:
                return await self._chat_once(
                    candidate, messages, tools, max_tokens
                )
            except LLMError as exc:
                last = index == len(chain) - 1
                if not exc.retryable or last:
                    raise
                log.warning(
                    "AI %s %s failed (%s) -> falling back to %s",
                    "model" if index else "primary model",
                    candidate,
                    exc.reason,
                    chain[index + 1],
                )

        raise LLMError("unreachable")  # pragma: no cover - loop always returns/raises

    async def _chat_once(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """One attempt against one model."""
        import httpx as _httpx

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self._settings.agent_max_tokens,
        }
        if tools:
            body["tools"] = tools
            # Keeps routing on a provider that honours every parameter sent. Without it,
            # `tools` is only a soft preference and a fallback provider can quietly drop
            # it, leaving the model to answer from nothing.
            body["provider"] = {"require_parameters": True}

        # Deliberately minimal. Unsupported parameters are silently dropped rather than
        # rejected -- claude-sonnet-5 does not advertise `temperature`, for instance --
        # so sending fewer of them is strictly safer.
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions", json=body
            )
        except (_httpx.TimeoutException, _httpx.TransportError) as exc:
            raise LLMTransientError(f"network failure talking to OpenRouter: {exc}") from exc

        if response.status_code == 404:
            raise ModelUnsupportedError(
                f"OpenRouter returned 404 for {model!r}. This usually means the "
                f"model does not exist or cannot use tools."
            )
        if response.status_code == 401:
            raise LLMError("OpenRouter rejected the API key (401).")
        if response.status_code == 429:
            raise LLMTransientError(
                f"OpenRouter rate limit hit for {model!r} (429)."
            )
        if response.status_code >= 500:
            raise LLMTransientError(
                f"OpenRouter server error for {model!r} ({response.status_code})."
            )
        if response.status_code >= 400:
            raise LLMError(f"OpenRouter returned {response.status_code}: {response.text[:400]}")

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"OpenRouter returned no choices: {payload}")
        return choices[0]
