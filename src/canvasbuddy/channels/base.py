"""The channel abstraction.

P0 only ever sends. ``receive`` is declared so the P1 chat agent has a shape to
implement against, but nothing calls it yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Button:
    label: str
    callback_data: str


@dataclass(frozen=True)
class IncomingMessage:
    channel: str
    text: str
    sender_id: str


@runtime_checkable
class Channel(Protocol):
    name: str

    async def send(self, text: str, buttons: list[Button] | None = None) -> str: ...

    def receive(self) -> AsyncIterator[IncomingMessage]: ...
