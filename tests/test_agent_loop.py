"""The tool-calling loop.

Every test here corresponds to a documented OpenRouter behaviour that produces a
confusing failure rather than a clean error.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from canvasbuddy.agent.loop import run_agent, validate_arguments
from canvasbuddy.agent.tools import TOOLS_BY_NAME
from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import LLMError, ModelUnsupportedError, OpenRouterClient

BASE = "https://openrouter.ai/api/v1"


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "openrouter_api_key": "sk-or-test",
        "agent_max_iterations": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def assistant_tool_call(name: str, arguments: dict | str, call_id: str = "call_1") -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}
            ],
        },
    }


def assistant_text(text: str) -> dict:
    return {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}


def responder(*choices: dict) -> Any:
    """Serve a scripted sequence of completions, recording each request body."""
    seen: list[dict] = []
    remaining = list(choices)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        choice = remaining.pop(0) if remaining else assistant_text("done")
        return httpx.Response(200, json={"choices": [choice]})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


@pytest.fixture
def patched_tools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace the real tools with recorders, so the loop is tested without a database."""
    calls: list[tuple[str, dict]] = []

    async def fake(session: object, settings: object, **kwargs: object) -> dict:
        calls.append(("list_upcoming", dict(kwargs)))
        return {"due": [{"course": "MGAB03", "title": "Group Project"}]}

    # Tool is frozen, so swap the registry entry rather than mutating the instance.
    monkeypatch.setitem(
        TOOLS_BY_NAME, "list_upcoming", replace(TOOLS_BY_NAME["list_upcoming"], fn=fake)
    )
    return calls


class TestValidateArguments:
    def test_accepts_valid(self) -> None:
        args, error = validate_arguments(TOOLS_BY_NAME["list_upcoming"], '{"days": 7}')
        assert error is None
        assert args == {"days": 7}

    def test_rejects_invalid_json(self) -> None:
        args, error = validate_arguments(TOOLS_BY_NAME["list_upcoming"], "{days: 7")
        assert args is None
        assert "valid JSON" in error

    def test_rejects_invented_parameters(self) -> None:
        """Models invent parameters. OpenRouter's docs are explicit this is unvalidated."""
        args, error = validate_arguments(
            TOOLS_BY_NAME["list_upcoming"], '{"days": 7, "sort_by": "urgency"}'
        )
        assert args is None
        assert "sort_by" in error

    def test_reports_missing_required(self) -> None:
        args, error = validate_arguments(TOOLS_BY_NAME["get_course"], "{}")
        assert args is None
        assert "code" in error

    def test_coerces_a_stringified_integer(self) -> None:
        """Models routinely send "7" for an integer; rejecting that would be pedantry."""
        args, error = validate_arguments(TOOLS_BY_NAME["list_upcoming"], '{"days": "7"}')
        assert error is None
        assert args == {"days": 7}

    def test_rejects_an_uncoercible_integer(self) -> None:
        args, error = validate_arguments(TOOLS_BY_NAME["list_upcoming"], '{"days": "soon"}')
        assert args is None
        assert "integer" in error

    def test_empty_arguments_are_an_empty_object(self) -> None:
        args, error = validate_arguments(TOOLS_BY_NAME["get_grades"], None)
        assert error is None
        assert args == {}


class TestLoop:
    @respx.mock
    async def test_single_tool_round_then_answer(self, patched_tools: list) -> None:
        handler = responder(
            assistant_tool_call("list_upcoming", {"days": 7}),
            assistant_text("You have the Group Project due Nov 15."),
        )
        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        messages: list[dict] = [{"role": "user", "content": "what's due"}]
        async with OpenRouterClient(make_settings()) as llm:
            reply = await run_agent(None, make_settings(), llm, messages)

        assert "Group Project" in reply.text
        assert reply.tools_used == ["list_upcoming"]
        assert patched_tools == [("list_upcoming", {"days": 7})]

    @respx.mock
    async def test_tools_are_resent_on_every_request(self, patched_tools: list) -> None:
        """OpenRouter revalidates the schema each call; dropping it strands the model."""
        handler = responder(
            assistant_tool_call("list_upcoming", {"days": 7}),
            assistant_text("done"),
        )
        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        async with OpenRouterClient(make_settings()) as llm:
            await run_agent(None, make_settings(), llm, [{"role": "user", "content": "hi"}])

        assert len(handler.seen) == 2
        assert handler.seen[0]["tools"], "first request must carry tools"
        assert handler.seen[1]["tools"], "follow-up must carry tools too"

    @respx.mock
    async def test_one_tool_message_per_parallel_call(self, patched_tools: list) -> None:
        """Anthropic rejects the conversation if any tool_call_id goes unanswered."""
        parallel = {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "list_upcoming", "arguments": '{"days": 7}'},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "list_upcoming", "arguments": '{"days": 30}'},
                    },
                ],
            },
        }
        handler = responder(parallel, assistant_text("both done"))
        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        async with OpenRouterClient(make_settings()) as llm:
            await run_agent(None, make_settings(), llm, [{"role": "user", "content": "hi"}])

        follow_up = handler.seen[1]["messages"]
        tool_messages = [m for m in follow_up if m.get("role") == "tool"]
        assert {m["tool_call_id"] for m in tool_messages} == {"call_a", "call_b"}

    @respx.mock
    async def test_prose_without_tool_calls_is_returned(self, patched_tools: list) -> None:
        """A fallback provider can accept `tools` and then ignore them."""
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [assistant_text("Hello.")]})
        )

        async with OpenRouterClient(make_settings()) as llm:
            reply = await run_agent(None, make_settings(), llm, [{"role": "user", "content": "hi"}])

        assert reply.text == "Hello."
        assert reply.tools_used == []

    @respx.mock
    async def test_bad_arguments_go_back_to_the_model(self, patched_tools: list) -> None:
        """A wrong guess must be correctable, not fatal to the conversation."""
        handler = responder(
            assistant_tool_call("list_upcoming", '{"days": 7, "nonsense": true}'),
            assistant_tool_call("list_upcoming", {"days": 7}, call_id="call_2"),
            assistant_text("Recovered."),
        )
        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        async with OpenRouterClient(make_settings()) as llm:
            reply = await run_agent(None, make_settings(), llm, [{"role": "user", "content": "hi"}])

        first_result = json.loads(
            next(m for m in handler.seen[1]["messages"] if m.get("role") == "tool")["content"]
        )
        assert "nonsense" in first_result["error"]
        assert reply.text == "Recovered."

    @respx.mock
    async def test_unknown_tool_name_is_reported_not_raised(self) -> None:
        handler = responder(
            assistant_tool_call("delete_everything", {}),
            assistant_text("I can't do that."),
        )
        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        async with OpenRouterClient(make_settings()) as llm:
            reply = await run_agent(None, make_settings(), llm, [{"role": "user", "content": "hi"}])

        result = json.loads(
            next(m for m in handler.seen[1]["messages"] if m.get("role") == "tool")["content"]
        )
        assert "No such tool" in result["error"]
        assert reply.text == "I can't do that."

    @respx.mock
    async def test_iteration_cap_forces_a_final_answer(self, patched_tools: list) -> None:
        """A model that never stops calling tools must still produce something."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            body = json.loads(request.content)
            if "tools" not in body:
                return httpx.Response(
                    200, json={"choices": [assistant_text("Here's what I found.")]}
                )
            return httpx.Response(
                200, json={"choices": [assistant_tool_call("list_upcoming", {"days": 7})]}
            )

        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        settings = make_settings(agent_max_iterations=3)
        async with OpenRouterClient(settings) as llm:
            reply = await run_agent(None, settings, llm, [{"role": "user", "content": "hi"}])

        assert reply.hit_iteration_cap
        assert reply.text == "Here's what I found."
        assert calls["n"] == 4  # three capped rounds, then one toolless call


class TestClientErrors:
    @respx.mock
    async def test_404_is_a_clear_model_error(self) -> None:
        """A tool-incapable model 404s rather than degrading."""
        respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(404, text="no"))

        async with OpenRouterClient(make_settings()) as llm:
            with pytest.raises(ModelUnsupportedError):
                await llm.chat([{"role": "user", "content": "hi"}], tools=[])

    @respx.mock
    async def test_401_is_reported_as_a_key_problem(self) -> None:
        respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(401, text="no"))

        async with OpenRouterClient(make_settings()) as llm:
            with pytest.raises(LLMError, match="API key"):
                await llm.chat([{"role": "user", "content": "hi"}])

    @respx.mock
    async def test_startup_check_rejects_an_unsupported_model(self) -> None:
        respx.get(f"{BASE}/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "anthropic/claude-sonnet-5"}]})
        )

        settings = make_settings(chat_model="some/model-without-tools")
        async with OpenRouterClient(settings) as llm:
            with pytest.raises(ModelUnsupportedError):
                await llm.assert_supports_tools(settings.chat_model)

    @respx.mock
    async def test_startup_check_passes_for_a_supported_model(self) -> None:
        respx.get(f"{BASE}/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "anthropic/claude-sonnet-5"}]})
        )

        settings = make_settings()
        async with OpenRouterClient(settings) as llm:
            await llm.assert_supports_tools(settings.chat_model)

    @respx.mock
    async def test_catalogue_outage_does_not_block_startup(self) -> None:
        """Failing to verify is not evidence the model is broken."""
        respx.get(f"{BASE}/models").mock(return_value=httpx.Response(503, text="down"))

        async with OpenRouterClient(make_settings()) as llm:
            await llm.assert_supports_tools("anthropic/claude-sonnet-5")

    @respx.mock
    async def test_tools_omitted_means_no_provider_pinning(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [assistant_text("ok")]})

        respx.post(f"{BASE}/chat/completions").mock(side_effect=handler)

        async with OpenRouterClient(make_settings()) as llm:
            await llm.chat([{"role": "user", "content": "hi"}], tools=None)

        assert "tools" not in seen[0]
        assert "provider" not in seen[0]
