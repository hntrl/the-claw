from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pipecat.frames.frames import DataFrame

MotionDirection = Literal["left", "right", "forward", "back", "down", "up"]


@dataclass
class UtteranceFrame(DataFrame):
    """Represents a user utterance entering the voice pipeline."""

    text: str
    source: str = "stdin"


@dataclass
class RawTextFrame(DataFrame):
    """Represents direct text input that should bypass STT."""

    text: str
    source: str = "text"


@dataclass
class AgentReplyFrame(DataFrame):
    """Assistant response after LLM reasoning/tool execution."""

    text: str
    tool_results: list[dict[str, Any]]
    interrupted: bool = False
    reset_to_attract: bool = False


@dataclass
class AssistantSpeechFrame(DataFrame):
    """Assistant speech frame split into TTS content and display metadata."""

    tts_text: str
    display_text: str
    cartesia_emotion: str | None
    speed_ratio: float | None
    volume_ratio: float | None
    interrupted: bool = False
    reset_to_attract: bool = False


@dataclass
class DisplayEventFrame(DataFrame):
    """Pipeline frame carrying a frontend websocket display event payload."""

    event: dict[str, Any]
