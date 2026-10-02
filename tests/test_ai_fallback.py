"""Multi-model fallback (PRD §22-23) and the secrets-to-AI guard (PRD §26).

Fallback exists for technical failures only. Every test here pins one edge of
that contract: what falls through, what fails fast, what never retries, and that
a successful primary keeps the fallbacks completely out of the request path.
"""

from __future__ import annotations

import json

import httpx
import respx

from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import LLMError, LLMTransientError, OpenRouterClient

OR = "https://openrouter.ai/api/v1"


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "openrouter_api_key": "sk-or-test",
        "chat_model": "primary/model",
        "ai_fallback_model_1": "fallback/one",
        "ai_fallback_model_2": "fallback/two",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def said(text: str) -> dict:
    return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}


def recorder(*responses):
    """A respx handler that serves responses in order and records request bodies."""
    seen: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if not queue:
            return httpx.Response(200, json=said("ok"))
        first = queue.pop(0)
        if isinstance(first, Exception):
            raise first
        return first

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def seen_models(handler) -> list[str]:
    return [body["model"] for body in handler.seen]


class TestFallbackChain:
    @respx.mock
    async def test_rate_limited_primary_falls_back(self) -> None:
        handler = recorder(
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json=said("from fallback one")),
        )
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            choice = await llm.chat([{"role": "user", "content": "hi"}])

        assert choice["message"]["content"] == "from fallback one"
        assert seen_models(handler) == ["primary/model", "fallback/one"]

    @respx.mock
    async def test_404_and_500_exhaust_to_fallback_two(self) -> None:
        handler = recorder(
            httpx.Response(404, json={"error": "no such model"}),
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json=said("from fallback two")),
        )
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            choice = await llm.chat([{"role": "user", "content": "hi"}])

        assert choice["message"]["content"] == "from fallback two"
        assert seen_models(handler) == ["primary/model", "fallback/one", "fallback/two"]

    @respx.mock
    async def test_timeout_falls_back(self) -> None:
        handler = recorder(
            httpx.ConnectTimeout("too slow"), httpx.Response(200, json=said("recovered"))
        )
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            choice = await llm.chat([{"role": "user", "content": "hi"}])

        assert choice["message"]["content"] == "recovered"
        assert seen_models(handler) == ["primary/model", "fallback/one"]

    @respx.mock
    async def test_all_models_failing_raises_transient(self) -> None:
        handler = recorder(
            httpx.Response(429, json={}),
            httpx.Response(429, json={}),
            httpx.Response(429, json={}),
        )
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            try:
                await llm.chat([{"role": "user", "content": "hi"}])
            except LLMTransientError:
                return
        raise AssertionError("expected LLMTransientError after exhausting the chain")

    @respx.mock
    async def test_rejected_key_fails_fast_without_fallback(self) -> None:
        """An invalid key is not a technical blip: every model in the chain would
        fail the same way, so burning the fallbacks is pure quota waste."""
        handler = recorder(httpx.Response(401, json={"error": "bad key"}))
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            try:
                await llm.chat([{"role": "user", "content": "hi"}])
            except LLMError as exc:
                assert not exc.retryable
                assert len(seen_models(handler)) == 1
                return
        raise AssertionError("expected LLMError for a 401")

    @respx.mock
    async def test_openrouter_invalid_model_400_falls_back(self) -> None:
        """OpenRouter reports an unknown model as a 400 ("not a valid model ID"),
        not a 404 -- observed live. The chain must treat it as model
        unavailability and slide to the next entry, across providers."""
        handler = recorder(
            httpx.Response(400, json={"error": {"message": "no such model"}}),
            httpx.Response(400, json={"error": {"message": "no such model"}}),
            httpx.Response(400, json={"error": {"message": "no such model"}}),
            httpx.Response(200, json=said("groq saved the day")),
        )
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        groq_handler = recorder(httpx.Response(200, json=said("groq saved the day")))
        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=groq_handler)

        settings = make_settings(
            groq_api_key="gsk-test",
            groq_model="openai/gpt-oss-120b",
        )
        async with OpenRouterClient(settings) as llm:
            choice = await llm.chat([{"role": "user", "content": "hi"}])

        assert choice["message"]["content"] == "groq saved the day"
        assert seen_models(handler) == [
            "primary/model",
            "fallback/one",
            "fallback/two",
        ]
        assert seen_models(groq_handler) == ["openai/gpt-oss-120b"]

    @respx.mock
    async def test_genuine_400_still_fails_fast(self) -> None:
        """A 400 that is NOT about model availability is a malformed request:
        no fallback, immediate error."""
        handler = recorder(
            httpx.Response(400, json={"error": {"message": "messages must be an array"}}),
        )
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            try:
                await llm.chat([{"role": "user", "content": "hi"}])
            except LLMError as exc:
                assert not exc.retryable
                assert len(seen_models(handler)) == 1
                return
        raise AssertionError("a malformed-request 400 should fail fast")

    @respx.mock
    async def test_successful_primary_never_calls_fallbacks(self) -> None:
        handler = recorder(httpx.Response(200, json=said("primary says hi")))
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            await llm.chat([{"role": "user", "content": "hi"}])
        assert len(seen_models(handler)) == 1

    @respx.mock
    async def test_explicit_model_is_used_alone(self) -> None:
        """Transcription and extraction name their own model: those calls must
        never wander into the chat fallback chain. A transient failure on an
        explicitly named model raises instead of substituting another model --
        the caller (transcription, extraction) owns that decision."""
        handler = recorder(httpx.Response(429, json={}))
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        async with OpenRouterClient(make_settings()) as llm:
            try:
                await llm.chat(
                    [{"role": "user", "content": "hi"}], model="google/gemini-2.5-flash-lite"
                )
            except LLMTransientError:
                assert seen_models(handler) == ["google/gemini-2.5-flash-lite"]
                return
        raise AssertionError("an explicit model should not have a fallback chain")

    @respx.mock
    async def test_missing_fallbacks_are_skipped(self) -> None:
        handler = recorder(httpx.Response(429, json={}), httpx.Response(200, json=said("ok")))
        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)
        settings = make_settings(ai_fallback_model_1=None, ai_fallback_model_2=None)
        async with OpenRouterClient(settings) as llm:
            try:
                await llm.chat([{"role": "user", "content": "hi"}])
            except LLMError:
                pass
        # With no fallbacks configured, only the primary was attempted.
        assert seen_models(handler) == ["primary/model"]


class TestSecretsNeverReachAI:
    @respx.mock
    async def test_canvas_token_and_keys_absent_from_ai_requests(self) -> None:
        """PRD §26: no Canvas token, Telegram token, Slack webhook or AI key may
        appear in anything sent to the provider, including the system prompt and
        every tool result."""
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=said("done"))

        respx.post(f"{OR}/chat/completions").mock(side_effect=handler)

        settings = make_settings(
            canvas_token="CANVAS-TOKEN-SECRET-123",
            telegram_bot_token="TG-TOKEN-SECRET-123",
            slack_webhook_url="https://hooks.slack.com/services/SECRET/WEBHOOK",
            openrouter_api_key="OR-KEY-SECRET-123",
        )
        from canvasbuddy.agent.loop import run_agent

        async def fake_list_upcoming(session, settings, **kwargs):
            return {
                "due": [
                    {"course": "CSC 153", "title": "Lab 5", "due_at": "2026-10-02T23:59-06:00"}
                ]
            }

        from dataclasses import replace as dc_replace

        from canvasbuddy.agent import tools as tools_mod

        original = tools_mod.TOOLS_BY_NAME["list_upcoming"]
        tools_mod.TOOLS_BY_NAME["list_upcoming"] = dc_replace(original, fn=fake_list_upcoming)
        try:
            async with OpenRouterClient(settings) as llm:
                await run_agent(None, settings, llm, [{"role": "user", "content": "what's due?"}])
        finally:
            tools_mod.TOOLS_BY_NAME["list_upcoming"] = original

        assert bodies, "the agent should have called the provider"
        for body in bodies:
            blob = json.dumps(body)
            for secret in (
                "CANVAS-TOKEN-SECRET-123",
                "TG-TOKEN-SECRET-123",
                "hooks.slack.com/services/SECRET/WEBHOOK",
                "OR-KEY-SECRET-123",
            ):
                assert secret not in blob, f"secret leaked to AI request: {secret}"
