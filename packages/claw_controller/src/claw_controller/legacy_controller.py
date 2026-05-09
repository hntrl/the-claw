"""
Hardware-backed claw controller.

Wraps the V2_agent firmware protocol (line-based, ACK/DONE/EVT/ERR/STATE) in an
async Python API suitable for use as a drop-in replacement for the simulated
ClawController in the voice-agent's app.py.

Design:
- A daemon reader thread pulls complete lines off the serial port and classifies
  them by first token (ACK / DONE / EVT / ERR / STATE / PONG / READY / INFO).
- Commands that expect completion (motion, homing) are tracked in a futures map
  keyed by command id. DONE / ERR from the firmware resolves them, and the
  async caller awaits with a timeout.
- Short commands (OPEN, CLOSE, S, EN, DIS, STATE?, PING) also go through the
  same correlation path for uniformity; the firmware sends ACK + DONE for all
  of them.
- State is cached locally, refreshed periodically via STATE? poll, and exposed
  synchronously via `state()` so the web UI's frequent polling does not trigger
  a serial round-trip every time.
- HALT bypasses the command queue: it is sent immediately and cancels any
  in-flight motion future with a HALTED result.
- Simulation mode: if CLAW_SIM=1 or the serial port cannot be opened, a purely
  in-memory simulation stands in. This lets the voice pipeline be developed and
  tested without the Arduino connected.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass, field

try:
    import serial  # type: ignore[import-untyped]
except Exception:  # noqa: BLE001
    serial = None  # type: ignore[assignment]
from pydantic import BaseModel, Field

log = logging.getLogger("claw_controller")

if serial is None:
    SERIAL_EXCEPTIONS: tuple[type[BaseException], ...] = (OSError, RuntimeError)
else:
    SERIAL_EXCEPTIONS = (serial.SerialException, OSError)  # type: ignore[union-attr]


# ============================================================================
# Pydantic models — kept compatible with app.py's original ClawState shape
# where possible, with additions for the real hardware.
# ============================================================================


class Limits(BaseModel):
    # Step counts at which each axis is considered at a hard limit.
    # These are informational for the agent; limit enforcement is done in
    # firmware via the physical switches.
    x_min: int = -1_000_000
    x_max: int = 1_000_000
    y_min: int = -1_000_000
    y_max: int = 1_000_000
    z_min: int = -1_000_000
    z_max: int = 1_000_000


class ClawState(BaseModel):
    # High-level firmware state machine: IDLE / MOVING_X / ... / HOMING_*
    fsm_state: str = "UNKNOWN"
    is_busy: bool = False

    # Stepper positions in firmware steps (not degrees). One full revolution
    # is 620 steps in the packaged V4 firmware.
    x: int = 0
    y: int = 0
    z: int = 0

    # Limit switch states (1 = triggered)
    limit_x: int = 0
    limit_a: int = 0
    limit_z: int = 0          # legacy field, no longer wired in V4 firmware
    limit_z_top: int = 0      # Z top limit (A0 / Abort header) — defines Z=0

    # Servo angle (physical range 25-90)
    servo_angle: int = 90

    # Motor driver enable
    motors_enabled: bool = True

    # Whether the firmware has a reliable Z=0 reference. Becomes True once the
    # Z top limit switch has fired at least once this power cycle (or was held
    # at boot). Until then, reported Z values are best-effort guesses based
    # on stepper-step counting alone, which the cable-spool design can drift
    # from due to tension shocks.
    z_homed: bool = False

    # Emergency stop latch — set locally on halt(), cleared by reset_emergency().
    # Firmware itself has no persistent emergency mode; this flag is enforced
    # in the controller to guard against new motion until the operator resets.
    emergency_stopped: bool = False

    last_error: str | None = None
    last_command_at: float | None = None
    limits: Limits = Field(default_factory=Limits)


# ============================================================================
# Command result — returned by every send_command call
# ============================================================================


@dataclass
class CommandResult:
    ok: bool
    status: str  # "OK" | "LIMIT" | "TIMEOUT" | "HALTED" | "ERR" | "SIM"
    command: str
    cmd_id: str
    events: list[str] = field(default_factory=list)
    error: str | None = None


# ============================================================================
# Hardware controller
# ============================================================================


class ClawController:
    """Async-friendly wrapper around the V2_agent firmware protocol."""

    # How long to wait for DONE before giving up. Homing can take a long time
    # because HOME_STEPS is huge; motion on a single axis is bounded by the
    # travel limit.
    DEFAULT_TIMEOUT_S = 15.0
    HOME_TIMEOUT_S = 60.0

    # STATE? poll interval (the reader thread also updates state from events,
    # but a periodic refresh catches anything that drifts).
    STATE_POLL_INTERVAL_S = 1.0

    # Steps per revolution — matches firmware/claw_machine_V4_agent.
    STEPS_PER_REV = 620
    STEPS_PER_DEGREE = STEPS_PER_REV / 360.0

    def __init__(
        self,
        port: str | None = None,
        baud: int = 115200,
        sim: bool | None = None,
    ) -> None:
        self.port = port or os.getenv("CLAW_SERIAL_PORT", "COM7")
        self.baud = int(os.getenv("CLAW_SERIAL_BAUD", baud))

        # Sim mode: explicit flag > env var > auto on port-open failure
        env_sim = os.getenv("CLAW_SIM", "").strip().lower() in {"1", "true", "yes"}
        self._sim = sim if sim is not None else env_sim

        self._ser: serial.Serial | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._write_lock = threading.Lock()

        # Command id generator (monotonic integer, wraps at a large number to
        # stay comfortably within Arduino String capacity).
        self._id_counter = itertools.count(1)

        # Pending command futures, keyed by command id string.
        # The main asyncio loop is captured when start() runs, so the reader
        # thread can schedule future resolutions on it via call_soon_threadsafe.
        self._pending: dict[str, asyncio.Future[CommandResult]] = {}
        self._pending_events: dict[str, list[str]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

        # Cached state. Default to IDLE with z_homed=False, which mirrors
        # the firmware's actual boot state when the Z top limit isn't held
        # at power-on. The first STATE? poll over the wire will refresh
        # this with whatever the firmware reports.
        self._state = ClawState()
        self._state.motors_enabled = True
        self._state.fsm_state = "IDLE"
        self._state.z_homed = False

        # Readiness flag set when firmware emits READY
        self._ready_event = threading.Event()

        # Periodic state refresher task handle
        self._state_poll_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open serial port, start reader thread, wait for firmware READY."""
        self._loop = asyncio.get_running_loop()

        if self._sim:
            log.warning("ClawController starting in SIMULATION mode")
            self._ready_event.set()
            self._state_poll_task = asyncio.create_task(self._state_poll_loop())
            return
        if serial is None:
            log.warning(
                "pyserial not installed; falling back to SIM mode for claw controller"
            )
            self._sim = True
            self._ready_event.set()
            self._state_poll_task = asyncio.create_task(self._state_poll_loop())
            return

        try:
            # dtr=False to reduce reset glitches on some boards; Arduino Uno
            # will still reset on port open, which is desirable — it means we
            # get a clean READY handshake every time.
            self._ser = serial.Serial()
            self._ser.port = self.port
            self._ser.baudrate = self.baud
            self._ser.timeout = 0.1  # short read timeout; reader loops on it
            self._ser.dtr = True
            self._ser.rts = True
            self._ser.open()
        except SERIAL_EXCEPTIONS as exc:
            log.warning("Could not open serial port %s: %s — falling back to SIM mode", self.port, exc)
            self._sim = True
            self._ready_event.set()
            self._state_poll_task = asyncio.create_task(self._state_poll_loop())
            return

        # Launch reader thread
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="claw-reader", daemon=True
        )
        self._reader_thread.start()

        # Wait for READY (up to ~5s — Uno reset + bootloader takes ~2s)
        ready = await asyncio.get_running_loop().run_in_executor(
            None, self._ready_event.wait, 5.0
        )
        if not ready:
            log.warning("Did not see READY from firmware within 5s; proceeding anyway")
        else:
            log.info("Firmware READY received")

        # Start periodic state poll
        self._state_poll_task = asyncio.create_task(self._state_poll_loop())

    async def close(self) -> None:
        """Stop reader thread, close serial port."""
        if self._state_poll_task:
            self._state_poll_task.cancel()
            try:
                await self._state_poll_task
            except asyncio.CancelledError:
                pass

        self._reader_stop.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)

        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Reader thread — parses firmware output into events and resolutions
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        assert self._ser is not None
        buf = b""
        while not self._reader_stop.is_set():
            try:
                chunk = self._ser.read(128)
            except SERIAL_EXCEPTIONS as exc:
                log.error("Serial read error: %s", exc)
                time.sleep(0.2)
                continue

            if not chunk:
                continue

            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line = line_bytes.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    self._handle_line(line)
                except Exception:  # noqa: BLE001
                    log.exception("Error handling firmware line: %r", line)

    def _handle_line(self, line: str) -> None:
        """Classify a firmware line by its leading token and dispatch."""
        log.debug("FIRMWARE <- %s", line)
        tokens = line.split()
        if not tokens:
            return

        head = tokens[0]

        if head == "READY":
            self._ready_event.set()
            return

        if head == "INFO":
            log.info("firmware info: %s", line)
            return

        if head == "ACK":
            # ACK <id> <cmd>
            if len(tokens) >= 3:
                cmd_id = tokens[1]
                self._pending_events.setdefault(cmd_id, []).append(line)
            return

        if head == "DONE":
            # DONE <id> <cmd> <status>
            if len(tokens) >= 4:
                cmd_id = tokens[1]
                cmd = tokens[2]
                status = tokens[3]
                self._resolve_future(
                    cmd_id,
                    CommandResult(
                        ok=status in {"OK", "LIMIT"},  # LIMIT is not an error for motion
                        status=status,
                        command=cmd,
                        cmd_id=cmd_id,
                        events=self._pending_events.pop(cmd_id, []),
                    ),
                )
            return

        if head == "ERR":
            # ERR <id> <reason>
            if len(tokens) >= 3:
                cmd_id = tokens[1]
                reason = " ".join(tokens[2:])
                self._resolve_future(
                    cmd_id,
                    CommandResult(
                        ok=False,
                        status="ERR",
                        command="",
                        cmd_id=cmd_id,
                        events=self._pending_events.pop(cmd_id, []),
                        error=reason,
                    ),
                )
            return

        if head == "EVT":
            # EVT <event> <key=value ...>
            # Update local state from events that change it.
            self._apply_event(tokens)
            # Attach to any in-flight command for its record.
            # Without a direct id on the event, we attach to the most recently
            # ACKed command. This is a soft correlation — fine for logging.
            if self._pending:
                latest_id = next(reversed(self._pending))
                self._pending_events.setdefault(latest_id, []).append(line)
            return

        if head == "STATE":
            # STATE <id> <fsm_state> X=.. Y=.. Z=.. ZMAX=.. ZREF=..
            #   ZHOMED=.. LX=.. LA=.. LY=.. LZTOP=.. SERVO=.. EN=..
            self._apply_state(tokens)
            # STATE? also gets a cmd_id for futures that awaited it.
            if len(tokens) >= 2:
                cmd_id = tokens[1]
                if cmd_id in self._pending:
                    self._resolve_future(
                        cmd_id,
                        CommandResult(
                            ok=True,
                            status="OK",
                            command="STATE?",
                            cmd_id=cmd_id,
                            events=self._pending_events.pop(cmd_id, []),
                        ),
                    )
            return

        if head == "PONG":
            # PONG <id>
            if len(tokens) >= 2:
                cmd_id = tokens[1]
                self._resolve_future(
                    cmd_id,
                    CommandResult(
                        ok=True,
                        status="OK",
                        command="PING",
                        cmd_id=cmd_id,
                        events=[],
                    ),
                )
            return

        log.debug("Unparsed firmware line: %s", line)

    def _apply_event(self, tokens: list[str]) -> None:
        """Apply mutations to cached state from EVT lines where possible."""
        # tokens = ["EVT", <event>, <k=v> ...]
        if len(tokens) < 2:
            return
        event = tokens[1]
        if event == "MOTORS":
            # "EVT MOTORS ENABLED" / "EVT MOTORS DISABLED"
            if len(tokens) >= 3:
                self._state.motors_enabled = tokens[2] == "ENABLED"
        elif event == "SERVO_TARGET":
            # "EVT SERVO_TARGET angle=<n>" — target, not current, but close enough
            for kv in tokens[2:]:
                if kv.startswith("angle="):
                    try:
                        self._state.servo_angle = int(kv.split("=", 1)[1])
                    except ValueError:
                        pass

    def _apply_state(self, tokens: list[str]) -> None:
        """Parse a STATE line and update cached state."""
        # STATE <id> <FSM_STATE> K=V K=V ...
        if len(tokens) < 3:
            return
        self._state.fsm_state = tokens[2]
        self._state.is_busy = tokens[2] != "IDLE"
        for kv in tokens[3:]:
            if "=" not in kv:
                continue
            key, val = kv.split("=", 1)
            try:
                ival = int(val)
            except ValueError:
                continue
            if key == "X":
                self._state.x = ival
            elif key == "Y":
                self._state.y = ival
            elif key == "Z":
                self._state.z = ival
            elif key == "LX":
                self._state.limit_x = ival
            elif key == "LA":
                self._state.limit_a = ival
            elif key == "LZ":
                self._state.limit_z = ival
            elif key == "LZTOP":
                self._state.limit_z_top = ival
            elif key == "SERVO":
                self._state.servo_angle = ival
            elif key == "EN":
                self._state.motors_enabled = bool(ival)
            elif key == "ZHOMED":
                self._state.z_homed = bool(ival)

    def _resolve_future(self, cmd_id: str, result: CommandResult) -> None:
        """Resolve a pending future from the reader thread (thread-safe)."""
        fut = self._pending.pop(cmd_id, None)
        if fut is None or self._loop is None:
            return
        if not fut.done():
            self._loop.call_soon_threadsafe(fut.set_result, result)

    # ------------------------------------------------------------------
    # Low-level command send — used by every public method
    # ------------------------------------------------------------------

    async def _send(
        self,
        command: str,
        *,
        timeout: float | None = None,
        expect_completion: bool = True,
    ) -> CommandResult:
        """Send a command line, await its DONE/ERR with a timeout."""
        if self._sim:
            return self._sim_send(command)

        cmd_id = str(next(self._id_counter))
        line = f"#{cmd_id} {command}\n"

        if expect_completion:
            fut: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
            self._pending[cmd_id] = fut

        # Write under a lock so two async callers don't interleave bytes.
        def _write() -> None:
            with self._write_lock:
                assert self._ser is not None
                self._ser.write(line.encode())
                self._ser.flush()

        try:
            await asyncio.to_thread(_write)
        except SERIAL_EXCEPTIONS as exc:
            self._pending.pop(cmd_id, None)
            return CommandResult(ok=False, status="ERR", command=command, cmd_id=cmd_id, error=str(exc))

        if not expect_completion:
            return CommandResult(ok=True, status="OK", command=command, cmd_id=cmd_id)

        try:
            return await asyncio.wait_for(fut, timeout=timeout or self.DEFAULT_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            return CommandResult(
                ok=False,
                status="TIMEOUT",
                command=command,
                cmd_id=cmd_id,
                error=f"No DONE within {timeout or self.DEFAULT_TIMEOUT_S}s",
            )

    def _sim_send(self, command: str) -> CommandResult:
        """Fake a command in simulation mode — updates local state only."""
        cmd_id = str(next(self._id_counter))
        parts = command.split()
        if not parts:
            return CommandResult(ok=False, status="ERR", command=command, cmd_id=cmd_id, error="empty")

        head = parts[0].upper()
        if head in {"X", "Y", "Z"} and len(parts) >= 2:
            try:
                degrees = float(parts[1])
            except ValueError:
                return CommandResult(ok=False, status="ERR", command=command, cmd_id=cmd_id)
            steps = int(degrees * self.STEPS_PER_DEGREE)
            if head == "X":
                self._state.x += steps
            elif head == "Y":
                self._state.y += steps
            elif head == "Z":
                self._state.z += steps
            return CommandResult(ok=True, status="SIM", command=head, cmd_id=cmd_id)
        if head == "OPEN":
            self._state.servo_angle = 90
            return CommandResult(ok=True, status="SIM", command="OPEN", cmd_id=cmd_id)
        if head == "CLOSE":
            self._state.servo_angle = 25
            return CommandResult(ok=True, status="SIM", command="CLOSE", cmd_id=cmd_id)
        if head == "S" and len(parts) >= 2:
            try:
                self._state.servo_angle = max(25, min(90, int(parts[1])))
            except ValueError:
                pass
            return CommandResult(ok=True, status="SIM", command="S", cmd_id=cmd_id)
        if head in {"ZHOME", "RAISE"}:
            # Simulate driving up to the top limit: instant resync.
            self._state.z = 0
            self._state.z_homed = True
            self._state.limit_z_top = 1
            return CommandResult(ok=True, status="SIM", command=head, cmd_id=cmd_id)
        if head == "HOME":
            self._state.x = 0
            self._state.y = 0
            self._state.z = 0
            self._state.z_homed = True
            self._state.limit_z_top = 1
            self._state.servo_angle = 90
            return CommandResult(ok=True, status="SIM", command="HOME", cmd_id=cmd_id)
        if head == "HALT":
            return CommandResult(ok=True, status="SIM", command="HALT", cmd_id=cmd_id)
        if head in {"STATE?", "STATE", "PING"}:
            return CommandResult(ok=True, status="SIM", command=head, cmd_id=cmd_id)
        return CommandResult(ok=True, status="SIM", command=head, cmd_id=cmd_id)

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------

    async def _state_poll_loop(self) -> None:
        """Refresh cached state periodically by issuing STATE?."""
        while True:
            try:
                await asyncio.sleep(self.STATE_POLL_INTERVAL_S)
                if self._sim:
                    continue
                await self._send("STATE?", timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("State poll error")

    # ------------------------------------------------------------------
    # Public API — matches what app.py expects
    # ------------------------------------------------------------------

    def state(self) -> ClawState:
        """Return cached state synchronously (fast, no serial I/O)."""
        return self._state.model_copy()

    async def refresh_state(self) -> ClawState:
        """Force a STATE? poll and return fresh state."""
        await self._send("STATE?", timeout=2.0)
        return self._state.model_copy()

    async def move_axis(self, axis: str, degrees: float) -> CommandResult:
        """Move a single axis by the given number of degrees.

        axis: 'x', 'y', or 'z'. Sign of degrees sets direction.
        """
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False,
                status="ERR",
                command=axis.upper(),
                cmd_id="-",
                error="Emergency stopped; call reset_emergency first",
            )

        axis_upper = axis.strip().upper()
        if axis_upper not in {"X", "Y", "Z"}:
            return CommandResult(
                ok=False, status="ERR", command=axis_upper, cmd_id="-", error=f"invalid axis {axis}"
            )

        self._state.last_command_at = time.monotonic()
        result = await self._send(f"{axis_upper} {degrees}")
        return result

    async def lower_claw(self, degrees: float) -> CommandResult:
        """Lower the claw by ``degrees`` of motor rotation (positive Z).

        Thin wrapper around move_axis('z', +degrees). The firmware enforces
        the Z bounds: a request that would exceed the safety floor is
        clamped, and the hardware top limit switch terminates upward
        motion regardless of step counting.

        ``degrees`` is treated as a magnitude — sign is forced positive
        because "lower" only makes sense as a downward movement. Calling
        with 0 or a negative number is a no-op (returns OK with no motion).
        """
        magnitude = abs(float(degrees))
        if magnitude == 0.0:
            return CommandResult(ok=True, status="OK", command="Z", cmd_id="-")
        return await self.move_axis("z", magnitude)

    async def raise_claw(self) -> CommandResult:
        """Retract the claw to the top (Z = 0), defined by the Z top limit.

        Sends the firmware's RAISE command (alias for ZHOME). Drives Z
        upward until the top limit switch fires and resyncs Z=0. If the
        switch never fires within HOME_PHASE_TIMEOUT_MS the firmware
        returns DONE RAISE STUCK.
        """
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False, status="ERR", command="RAISE", cmd_id="-", error="Emergency stopped"
            )
        self._state.last_command_at = time.monotonic()
        return await self._send("RAISE")

    async def open_claw(self, angle: int | None = None) -> CommandResult:
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False, status="ERR", command="OPEN", cmd_id="-", error="Emergency stopped"
            )
        self._state.last_command_at = time.monotonic()
        cmd = "OPEN" if angle is None else f"OPEN {int(angle)}"
        return await self._send(cmd, timeout=3.0)

    async def close_claw(self, angle: int | None = None) -> CommandResult:
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False, status="ERR", command="CLOSE", cmd_id="-", error="Emergency stopped"
            )
        self._state.last_command_at = time.monotonic()
        cmd = "CLOSE" if angle is None else f"CLOSE {int(angle)}"
        return await self._send(cmd, timeout=3.0)

    async def set_servo(self, angle: int) -> CommandResult:
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False, status="ERR", command="S", cmd_id="-", error="Emergency stopped"
            )
        self._state.last_command_at = time.monotonic()
        return await self._send(f"S {int(angle)}", timeout=3.0)

    async def home(self) -> CommandResult:
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False, status="ERR", command="HOME", cmd_id="-", error="Emergency stopped"
            )
        self._state.last_command_at = time.monotonic()
        return await self._send("HOME", timeout=self.HOME_TIMEOUT_S)

    async def halt(self) -> CommandResult:
        """Emergency halt — stops any in-flight motion and latches e-stop."""
        self._state.emergency_stopped = True
        self._state.last_command_at = time.monotonic()

        # Cancel any pending futures immediately (they will also receive
        # HALTED from the firmware, but we don't want the caller to block).
        for cmd_id, fut in list(self._pending.items()):
            if not fut.done():
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        fut.set_result,
                        CommandResult(
                            ok=False, status="HALTED", command="?", cmd_id=cmd_id,
                            error="halted by controller",
                        ),
                    )

        # Send HALT. It has short timeout because firmware responds immediately.
        return await self._send("HALT", timeout=3.0)

    async def reset_emergency(self) -> CommandResult:
        """Clear the e-stop latch so new motion commands are accepted."""
        self._state.emergency_stopped = False
        self._state.last_command_at = time.monotonic()
        return CommandResult(ok=True, status="OK", command="RESET", cmd_id="-")

    async def home_z(self) -> CommandResult:
        """Drive the claw up until the Z top limit switch fires.

        Sends ZHOME to the firmware. On success, Z is mechanically at the
        top, zPositionSteps is reset to 0, and z_homed becomes True. If
        the switch never fires within the firmware's phase timeout, the
        result is ok=False with status="STUCK".

        Use cases:
          - First-run after powering on (the firmware boots without a
            known Z reference unless the switch is held at boot).
          - Recovering after suspected step-skipping (cable spool tension
            shocks can cause the motor's notion of position to drift from
            physical reality; ZHOME resyncs).
        """
        if self._state.emergency_stopped:
            return CommandResult(
                ok=False, status="ERR", command="ZHOME", cmd_id="-", error="Emergency stopped"
            )
        self._state.last_command_at = time.monotonic()
        result = await self._send("ZHOME", timeout=30.0)
        # Refresh state so the UI reflects the new homed status quickly,
        # rather than waiting for the next periodic STATE? poll.
        if result.ok:
            try:
                await self._send("STATE?", timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        return result

    async def ping(self) -> CommandResult:
        return await self._send("PING", timeout=2.0)
