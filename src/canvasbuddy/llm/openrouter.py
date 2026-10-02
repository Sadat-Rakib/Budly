"""The AI provider chain.

Hand-rolled on ``httpx`` rather than the ``openai`` SDK, and the reason is a hard
constraint rather than a preference: ``openai`` 3.x depends on ``httpx2``, a separate HTTP
stack that cannot coexist cleanly with the ``httpx <0.29`` that python-telegram-bot pins.
Taking the SDK would mean two HTTP clients, two connection pools and two TLS
configurations in one image, to gain typed wrappers around endpoints whose
messages we have to hand back as plain dicts anyway.

The client walks a chain of OpenAI-compatible **provider endpoints** (PRD §21-22):
the OpenRouter models first, then Groq, then whatever else a deployment adds. The
point is the "never run out" property: every provider in the chain has its own
free tier and its own rate-limit bucket, so a 429 on one slides to the next, and
deterministic answers wait at the bottom no matter what.

Three details of OpenAI-compatible tool calling are easy to get wrong and are
handled here:

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
from dataclasses import dataclass
from typing import Any

import httpx

from canvasbuddy.config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any unrecoverable failure talking to an AI provider."""

    #: False for failures where trying another provider cannot help (a rejected key,
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
    or a malformed request is not here: retrying those with another provider just
    burns quota on a failure that will repeat.
    """

    retryable = True
    reason = "transient"


@dataclass(frozen=True)
class ProviderEndpoint:
    """One OpenAI-compatible endpoint in the fallback chain."""

    provider: str  # "openrouter", "groq", ... -- for logs and headers
    base_url: str
    api_key: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def build_provider_chain(settings: Settings) -> list[ProviderEndpoint]:
    """The ordered endpoint chain, from the deployment's configured keys.

    Every provider with a key contributes its slot; the chain is what makes
    "never run out of tokens" real -- separate providers mean separate free
    tiers and separate rate-limit buckets.
    """
    chain: list[ProviderEndpoint] = []
    if settings.openrouter_api_key is not None:
        openrouter = settings.openrouter_api_key.get_secret_value()
        for model in (
            settings.chat_model,
            settings.ai_fallback_model_1,
            settings.ai_fallback_model_2,
        ):
            if model:
                chain.append(
                    ProviderEndpoint(
                        "openrouter", settings.openrouter_base_url, openrouter, model
                    )
                )
    if settings.groq_api_key is not None:
        chain.append(
            ProviderEndpoint(
                "groq",
                settings.groq_base_url,
                settings.groq_api_key.get_secret_value(),
                settings.groq_model,
            )
        )
    return chain


class OpenRouterClient:
    """The AI client. The name is historical; it drives the whole endpoint chain."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        # No default Authorization header: every endpoint in the chain carries its
        # own key, attached per request.
        self._client = client or httpx.AsyncClient(
            # A tool round-trip through a large model is slow; the connect timeout stays
            # short so a dead network still fails fast.
            timeout=httpx.Timeout(120.0, connect=10.0),
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
        exact moment someone asks the bot a question. Only the OpenRouter primary is
        checked; later chain entries fail over at request time by design.
        """
        primary = build_provider_chain(self._settings)[:1]
        if not primary:
            return
        response = await self._client.get(
            f"{primary[0].base_url}/models", params={"supported_parameters": "tools"}
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
        """One completion, with fallback across the provider chain.

        The chain is primary → fallback models → next provider (PRD §22). Only
        technical failures move down the chain -- rate limit, outage, timeout,
        model gone. A rejected key or malformed request raises immediately, and a
        successful earlier entry never touches the later ones (no wasted quota,
        PRD §23). When an explicit ``model`` was requested, only the primary
        endpoint serves it.
        """
        if model:
            primary = build_provider_chain(self._settings)[:1]
            if not primary:
                raise LLMError("OPENROUTER_API_KEY is not set.")
            chain: list[ProviderEndpoint] = [
                ProviderEndpoint(
                    primary[0].provider, primary[0].base_url, primary[0].api_key, model
                )
            ]
        else:
            chain = build_provider_chain(self._settings)
        if not chain:
            raise LLMError("OPENROUTER_API_KEY is not set.")

        for index, endpoint in enumerate(chain):
            try:
                return await self._chat_once(endpoint, messages, tools, max_tokens)
            except LLMError as exc:
                last = index == len(chain) - 1
                if not exc.retryable or last:
                    raise
                log.warning(
                    "AI %s failed (%s) -> falling back to %s",
                    endpoint.label,
                    exc.reason,
                    chain[index + 1].label,
                )

        raise LLMError("unreachable")  # pragma: no cover - loop always returns/raises

    async def _chat_once(
        self,
        endpoint: ProviderEndpoint,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """One attempt against one endpoint."""
        import httpx as _httpx

        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        if endpoint.provider == "openrouter":
            # Optional -- they only affect how this app appears in OpenRouter's own
            # activity log, which is worth having when debugging a bad turn.
            headers["HTTP-Referer"] = "https://github.com/Sadat-Rakib/StudyBuddy"
            headers["X-Title"] = "Budly"

        body: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
            "max_tokens": max_tokens or self._settings.agent_max_tokens,
        }
        if tools:
            body["tools"] = tools
            if endpoint.provider == "openrouter":
                # Keeps routing on a provider that honours every parameter sent. Without it,
                # `tools` is only a soft preference and a fallback provider can quietly drop
                # it, leaving the model to answer from nothing. OpenRouter-specific.
                body["provider"] = {"require_parameters": True}

        # Deliberately minimal. Unsupported parameters are silently dropped rather than
        # rejected -- claude-sonnet-5 does not advertise `temperature`, for instance --
        # so sending fewer of them is strictly safer.
        try:
            response = await self._client.post(
                f"{endpoint.base_url.rstrip('/')}/chat/completions", json=body, headers=headers
            )
        except (_httpx.TimeoutException, _httpx.TransportError) as exc:
            raise LLMTransientError(
                f"network failure talking to {endpoint.provider}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise ModelUnsupportedError(
                f"{endpoint.provider} returned 404 for {endpoint.model!r}. This usually "
                f"means the model does not exist or cannot use tools."
            )
        if response.status_code == 401:
            raise LLMError(
                f"{endpoint.provider} rejected the API key (401)."
            )
        if response.status_code == 400:
            # OpenRouter reports an unknown model as a 400 ("not a valid model ID"),
            # not a 404, and a :free model whose providers are all saturated comes
            # back as "no allowed providers". Both are model-availability problems:
            # exactly what the next chain entry exists for. Anything else in a 400
            # is a malformed request and fails fast.
            body_text = response.text[:600].lower()
            model_markers = (
                "not a valid model",
                "no such model",
                "model not found",
                "does not exist",
                "no allowed providers",
                "no endpoints found",
                "model is not available",
            )
            if any(marker in body_text for marker in model_markers):
                raise ModelUnsupportedError(
                    f"{endpoint.provider} has no usable {endpoint.model!r} (400)."
                )
        if response.status_code == 429:
            raise LLMTransientError(
                f"{endpoint.provider} rate limit hit for {endpoint.model!r} (429)."
            )
        if response.status_code >= 500:
            raise LLMTransientError(
                f"{endpoint.provider} server error for {endpoint.model!r} "
                f"({response.status_code})."
            )
        if response.status_code >= 400:
            raise LLMError(
                f"{endpoint.provider} returned {response.status_code}: {response.text[:400]}"
            )

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"{endpoint.provider} returned no choices: {payload}")
        return choices[0]


#: The PRD §21 interface name, kept as an alias: the chain *is* the AIProvider.
AIProvider = OpenRouterClient
