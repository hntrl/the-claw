from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

MotionDirection = Literal["left", "right", "forward", "back", "down", "up"]
ExecutionOutcome = Literal["pending", "success", "failure"]
TurnStatus = Literal["pending", "running", "interrupted", "completed", "error"]
ControllerMode = Literal["sim", "serial"]

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
InterruptChecker = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class TurnState:
    turn_id: str
    target_label: str
    motions: list[MotionDirection]
    status: TurnStatus = "pending"
    interrupted: bool = False
    outcome: ExecutionOutcome = "pending"
    started_at_s: float | None = None
    finished_at_s: float | None = None
    error: str | None = None


@dataclass(slots=True)
class ControllerState:
    mode: ControllerMode
    connected: bool = False
    busy: bool = False
    last_error: str | None = None
    last_command_at_s: float | None = None
    current_turn: TurnState | None = None
    last_turn: TurnState | None = None


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    interrupted: bool
    target_label: str
    motions: list[MotionDirection]
    outcome: ExecutionOutcome
    error: str | None = None
