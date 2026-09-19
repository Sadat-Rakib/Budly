"""The tool-calling loop.

OpenRouter speaks OpenAI's shape, which has three edges worth naming because each one
produces a confusing failure rather than a clean error:

* ``tools`` must be sent on **every** request. OpenRouter revalidates the schema each
  call, so dropping it on the follow-up leaves the model unable to finish what it started.
* ``arguments`` arrives as a **string** of model-generated JSON. OpenRouter's own docs
  warn it is not a validated payload, and models do invent parameters that were never
  declared.
* Parallel calls require one ``role:"tool"`` message per ``tool_call_id`` -- all of them,
  before the next request -- or Anthropic upstream rejects the conversation with a 400.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.agent.tools import TOOLS_BY_NAME, Tool, tool_schemas
from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient

log = logging.getLogger(__name__)


@dataclass
class AgentReply:
    text: str
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    hit_iteration_cap: bool = False


def validate_arguments(tool: Tool, raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and check one tool call's arguments.

    Returns ``(arguments, None)`` or ``(None, error_message)``. Errors are returned rather
    than raised so they can be handed back to the model, which will usually correct itself
    on the next round -- far better than failing the whole conversation because the model
    guessed a parameter name.
    """
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return None, f"arguments were not valid JSON: {exc}"

    if not isinstance(parsed, dict):
        return None, "arguments must be a JSON object"

    properties: dict[str, Any] = tool.parameters.get("properties", {})
    unknown = set(parsed) - set(properties)
    if unknown:
        return None, (
            f"unknown parameter(s): {', '.join(sorted(unknown))}. "
            f"Valid parameters are: {', '.join(sorted(properties)) or 'none'}."
        )

    missing = [name for name in tool.parameters.get("required", []) if name not in parsed]
    if missing:
        return None, f"missing required parameter(s): {', '.join(missing)}"

    # Light coercion only. Models reliably send "7" where 7 was asked for, and rejecting
    # that would be pedantry; anything genuinely wrong still fails here.
    coerced: dict[str, Any] = {}
    for name, value in parsed.items():
        expected = properties[name].get("type")
        if expected == "integer" and not isinstance(value, bool):
            try:
                coerced[name] = int(value)
                continue
            except (TypeError, ValueError):
                return None, f"{name!r} must be an integer, got {value!r}"
        if expected == "string" and value is not None and not isinstance(value, str):
            coerced[name] = str(value)
            continue
        coerced[name] = value

    return coerced, None


async def _execute(
    session: AsyncSession, settings: Settings, name: str, raw_arguments: str | None
) -> Any:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"error": f"No such tool {name!r}. Available: {', '.join(sorted(TOOLS_BY_NAME))}."}

    arguments, error = validate_arguments(tool, raw_arguments)
    if error is not None:
        log.info("Rejected call to %s: %s", name, error)
        return {"error": error}

    try:
        return await tool.fn(session, settings, **(arguments or {}))
    except Exception as exc:  # noqa: BLE001 - the model gets the error and can adapt
        log.exception("Tool %s raised", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def run_agent(
    session: AsyncSession,
    settings: Settings,
    llm: OpenRouterClient,
    messages: list[dict[str, Any]],
) -> AgentReply:
    """Run the conversation to an answer.

    ``messages`` is mutated in place so the caller can persist the full exchange,
    including the tool traffic, after the reply is produced.
    """
    schemas = tool_schemas()
    used: list[str] = []

    for iteration in range(1, settings.agent_max_iterations + 1):
        choice = await llm.chat(messages, tools=schemas)
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        # No tool calls means the model has answered. This also catches the case where a
        # provider accepted `tools` and then ignored them -- we get prose, and returning
        # it is better than insisting on a tool round that will never come.
        if not tool_calls:
            return AgentReply(
                text=(message.get("content") or "").strip(),
                tools_used=used,
                iterations=iteration,
            )

        # The assistant message goes back verbatim: the ids in it are what the tool
        # results below are matched against.
        messages.append(message)

        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            used.append(name)
            result = await _execute(session, settings, name, function.get("arguments"))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result, default=str),
                }
            )

    # Out of iterations. Rather than returning nothing, ask once more with no tools so the
    # model has to answer from what it already gathered.
    log.warning("Agent hit the %d-iteration cap", settings.agent_max_iterations)
    try:
        choice = await llm.chat(messages, tools=None)
        text = (choice.get("message", {}).get("content") or "").strip()
    except LLMError:
        text = ""

    return AgentReply(
        text=text or "I looked at a few things but couldn't pull that together. Try narrowing it?",
        tools_used=used,
        iterations=settings.agent_max_iterations,
        hit_iteration_cap=True,
    )
