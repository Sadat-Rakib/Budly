"""Channel-agnostic notification content."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Section:
    heading: str
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NotificationContent:
    kind: str  # digest|nudge|review|checkin
    title: str
    sections: list[Section] = field(default_factory=list)
    footer: str = ""
