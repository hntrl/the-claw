from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .types import ExecutionOutcome

ResultMode = Literal["pending", "random"]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


@dataclass(slots=True)
class ClawControllerConfig:
    mode: Literal["auto", "sim", "serial"] = "auto"
    serial_port: str = "/dev/tty.usbmodem"
    serial_baud: int = 115200
    serial_timeout_s: float = 1.0

    x_move_degrees: float = 90.0
    y_move_degrees: float = 90.0
    z_down_degrees: float = 360.0
    z_up_degrees: float = 180.0
    use_raise_for_up: bool = True

    motion_delay_s: float = 0.42
    settle_delay_s: float = 0.34
    reset_delay_s: float = 1.2

    result_mode: ResultMode = "pending"
    success_rate: float = 0.68

    @classmethod
    def from_env(cls, *, success_rate: float = 0.68) -> "ClawControllerConfig":
        mode_raw = os.getenv("CLAW_CONTROLLER_MODE", "auto").strip().lower()
        if mode_raw not in {"auto", "sim", "serial"}:
            mode_raw = "auto"

        result_mode_raw = os.getenv("CLAW_RESULT_MODE", "pending").strip().lower()
        if result_mode_raw not in {"pending", "random"}:
            result_mode_raw = "pending"

        success_rate_raw = _env_float("CLAW_RESULT_SUCCESS_RATE", success_rate)
        success_rate_raw = max(0.0, min(1.0, success_rate_raw))

        return cls(
            mode=mode_raw,
            serial_port=os.getenv("CLAW_SERIAL_PORT", "/dev/tty.usbmodem").strip() or "/dev/tty.usbmodem",
            serial_baud=_env_int("CLAW_SERIAL_BAUD", 115200),
            serial_timeout_s=_env_float("CLAW_SERIAL_TIMEOUT_S", 1.0),
            x_move_degrees=_env_float("CLAW_MOVE_X_DEGREES", 90.0),
            y_move_degrees=_env_float("CLAW_MOVE_Y_DEGREES", 90.0),
            z_down_degrees=_env_float("CLAW_MOVE_Z_DOWN_DEGREES", 360.0),
            z_up_degrees=_env_float("CLAW_MOVE_Z_UP_DEGREES", 180.0),
            use_raise_for_up=_env_bool("CLAW_USE_RAISE_FOR_UP", True),
            motion_delay_s=_env_int("AGENT_MOTION_DELAY_MS", 420) / 1000,
            settle_delay_s=_env_int("AGENT_SETTLE_DELAY_MS", 340) / 1000,
            reset_delay_s=_env_int("AGENT_RESET_TO_ATTRACT_MS", 1200) / 1000,
            result_mode=result_mode_raw,
            success_rate=success_rate_raw,
        )

    def choose_outcome(self, random_value: float) -> ExecutionOutcome:
        if self.result_mode != "random":
            return "pending"
        return "success" if random_value < self.success_rate else "failure"
