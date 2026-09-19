"""Turning a Telegram voice note into text.

Telegram sends voice messages as OGG/Opus. OpenRouter accepts ``ogg`` directly as an
``input_audio`` content part, so the file goes straight from Telegram to the model --
no ffmpeg in the container, no format conversion, and no second provider on top of the
OpenRouter key that already exists.
"""

from __future__ import annotations

import base64
import logging

from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient

log = logging.getLogger(__name__)

#: Telegram caps voice notes at about a minute for most clients; this is a guard against
#: a forwarded audio file of arbitrary length being sent to a model.
MAX_AUDIO_BYTES = 20 * 1024 * 1024

_PROMPT = (
    "Transcribe this voice message verbatim. Return only the transcription, with no "
    "preamble, no quotation marks, and no commentary. If the audio is silent or "
    "unintelligible, return exactly: [unintelligible]"
)


class TranscriptionError(LLMError):
    pass


async def transcribe(
    settings: Settings, llm: OpenRouterClient, audio: bytes, audio_format: str = "ogg"
) -> str:
    """Transcribe audio bytes. Raises TranscriptionError if nothing usable comes back."""
    if not audio:
        raise TranscriptionError("empty audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionError("that audio is too long for me to transcribe")

    encoded = base64.b64encode(audio).decode()
    choice = await llm.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": encoded, "format": audio_format},
                    },
                ],
            }
        ],
        tools=None,
        model=settings.transcription_model,
        max_tokens=1024,
    )

    text = (choice.get("message", {}).get("content") or "").strip()
    if not text or text == "[unintelligible]":
        raise TranscriptionError("I couldn't make that out - try again?")
    return text
