from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from serial.tools import list_ports

from .types import ExecutionOutcome

log = logging.getLogger(__name__)

_DEFAULT_SERIAL_PORT = "/dev/tty.usbmodem"
_USB_SERIAL_PREFIXES = (
    "/dev/cu.usbmodem",
    "/dev/cu.usbserial",
    "/dev/tty.usbmodem",
    "/dev/tty.usbserial",
    "/dev/ttyACM",
    "/dev/ttyUSB",
    "COM",
)

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


def _env_usb_id(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            log.warning("Ignoring invalid %s=%r; use a decimal or hexadecimal USB ID", name, value)
            return None


def _discover_serial_port() -> str:
    vid = _env_usb_id("CLAW_SERIAL_VID")
    pid = _env_usb_id("CLAW_SERIAL_PID")
    match = os.getenv("CLAW_SERIAL_MATCH", "").strip().lower()
    candidates = []

    for port in list_ports.comports():
        if not port.device.startswith(_USB_SERIAL_PREFIXES):
            continue
        if vid is not None and port.vid != vid:
            continue
        if pid is not None and port.pid != pid:
            continue
        details = " ".join(
            str(value or "")
            for value in (port.device, port.description, port.manufacturer, port.product, port.hwid)
        ).lower()
        if match and match not in details:
            continue
        candidates.append(port)

    arduino_candidates = [
        port
        for port in candidates
        if "arduino" in " ".join(
            str(value or "") for value in (port.description, port.manufacturer, port.product)
        ).lower()
    ]
    if len(arduino_candidates) == 1:
        candidates = arduino_candidates

    if len(candidates) == 1:
        port = candidates[0]
        log.info("Auto-discovered claw serial device: %s (%s)", port.device, port.description)
        return port.device

    if not candidates:
        log.warning("No matching USB serial device found for claw controller")
    else:
        devices = ", ".join(sorted(port.device for port in candidates))
        log.warning(
            "Multiple matching USB serial devices found (%s); set CLAW_SERIAL_PORT, "
            "CLAW_SERIAL_VID/CLAW_SERIAL_PID, or CLAW_SERIAL_MATCH to choose one",
            devices,
        )
    return ""


def _serial_port_from_env() -> str:
    port = os.getenv("CLAW_SERIAL_PORT", "auto").strip()
    if port and port not in {"auto", _DEFAULT_SERIAL_PORT}:
        return port
    return _discover_serial_port()


@dataclass(slots=True)
class ClawControllerConfig:
    mode: Literal["auto", "sim", "serial"] = "auto"
    serial_port: str = ""
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
            serial_port=_serial_port_from_env(),
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
