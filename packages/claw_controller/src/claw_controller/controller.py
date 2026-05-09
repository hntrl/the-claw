from __future__ import annotations

import asyncio
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any, Protocol

from .config import ClawControllerConfig
from .legacy_controller import ClawController as LegacyClawController
from .legacy_controller import ClawState as LegacyClawState
from .legacy_controller import CommandResult as LegacyCommandResult
from .types import (
    ControllerState,
    EventEmitter,
    ExecutionResult,
    ExecutionOutcome,
    InterruptChecker,
    MotionDirection,
    TurnState,
)


class ClawControllerBackend(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def snapshot(self) -> ControllerState: ...

    def estimate_duration_s(self, motions: list[MotionDirection]) -> float: ...

    async def execute_plan(
        self,
        *,
        target_label: str,
        motions: list[MotionDirection],
        should_interrupt: InterruptChecker,
        on_event: EventEmitter,
    ) -> ExecutionResult: ...

    async def get_machine_state(self) -> LegacyClawState: ...

    async def move_axis(self, axis: str, degrees: float) -> LegacyCommandResult: ...

    async def lower_claw(self, degrees: float) -> LegacyCommandResult: ...

    async def raise_claw(self) -> LegacyCommandResult: ...

    async def open_claw(self, angle: int | None = None) -> LegacyCommandResult: ...

    async def close_claw(self, angle: int | None = None) -> LegacyCommandResult: ...

    async def home(self) -> LegacyCommandResult: ...

    async def home_z(self) -> LegacyCommandResult: ...

    async def halt(self) -> LegacyCommandResult: ...

    async def reset_emergency(self) -> LegacyCommandResult: ...


def _format_command_error(result: LegacyCommandResult) -> str:
    if result.error:
        return result.error
    return f"{result.command}:{result.status}"


def _result_to_dict(
    result: LegacyCommandResult, *, state: LegacyClawState | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": result.ok,
        "status": result.status,
        "command": result.command,
    }
    if result.error:
        payload["error"] = result.error
    if result.events:
        payload["events"] = result.events
    if state is not None:
        payload["state"] = state.model_dump()
    return payload


class _BaseExecutionController(ABC):
    def __init__(self, config: ClawControllerConfig, *, mode: str) -> None:
        self._config = config
        self._mode = mode
        self._state = ControllerState(mode="serial" if mode == "serial" else "sim")
        self._state_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._set_connected(connected=True, error=None)

    async def stop(self) -> None:
        await self._set_connected(connected=False, error=None)

    async def snapshot(self) -> ControllerState:
        async with self._state_lock:
            return replace(
                self._state,
                current_turn=self._clone_turn(self._state.current_turn),
                last_turn=self._clone_turn(self._state.last_turn),
            )

    def estimate_duration_s(self, motions: list[MotionDirection]) -> float:
        return round(len(motions) * self._config.motion_delay_s, 2)

    async def execute_plan(
        self,
        *,
        target_label: str,
        motions: list[MotionDirection],
        should_interrupt: InterruptChecker,
        on_event: EventEmitter,
    ) -> ExecutionResult:
        turn = TurnState(
            turn_id=uuid.uuid4().hex,
            target_label=target_label,
            motions=list(motions),
            status="running",
            started_at_s=time.monotonic(),
        )
        await self._set_turn_started(turn)

        emitted_drop_state = False
        try:
            await on_event({"type": "state", "state": "moving"})

            for motion in motions:
                if await should_interrupt():
                    return await self._finish_interrupted(turn)

                if motion == "down" and not emitted_drop_state:
                    emitted_drop_state = True
                    await on_event({"type": "state", "state": "dropping"})
                    await on_event({"type": "effect", "effect": "dropStarted"})

                error = await self._run_motion(motion)
                if error:
                    return await self._finish_error(turn, error=error)

                await on_event(
                    {
                        "type": "claw_motion",
                        "direction": motion,
                        "speed": 0.55 if motion == "down" else 0.8,
                    }
                )

                if await self._sleep_or_interrupt(
                    self._config.motion_delay_s, should_interrupt
                ):
                    return await self._finish_interrupted(turn)

            if not emitted_drop_state:
                await on_event({"type": "state", "state": "dropping"})
                await on_event({"type": "effect", "effect": "dropStarted"})
                if await self._sleep_or_interrupt(
                    self._config.settle_delay_s, should_interrupt
                ):
                    return await self._finish_interrupted(turn)

            if await self._sleep_or_interrupt(self._config.settle_delay_s, should_interrupt):
                return await self._finish_interrupted(turn)

            outcome = self._config.choose_outcome(random.random())
            if outcome in {"success", "failure"}:
                await on_event(
                    {
                        "type": "result",
                        "outcome": outcome,
                        "label": target_label,
                    }
                )

            if not await self._sleep_or_interrupt(self._config.reset_delay_s, should_interrupt):
                await on_event({"type": "state", "state": "attract"})

            return await self._finish_completed(turn, outcome=outcome)
        except Exception as exc:  # noqa: BLE001
            return await self._finish_error(turn, error=str(exc))

    @abstractmethod
    async def _run_motion(self, motion: MotionDirection) -> str | None:
        pass

    async def _sleep_or_interrupt(
        self, delay_s: float, should_interrupt: InterruptChecker
    ) -> bool:
        slice_s = 0.05
        remaining = delay_s
        while remaining > 0:
            if await should_interrupt():
                return True
            current = min(slice_s, remaining)
            await asyncio.sleep(current)
            remaining -= current
        return await should_interrupt()

    async def _set_connected(self, *, connected: bool, error: str | None) -> None:
        async with self._state_lock:
            self._state.connected = connected
            self._state.last_error = error

    async def _set_last_error(self, error: str | None) -> None:
        async with self._state_lock:
            self._state.last_error = error

    async def _set_turn_started(self, turn: TurnState) -> None:
        async with self._state_lock:
            self._state.busy = True
            self._state.current_turn = self._clone_turn(turn)

    async def _record_command_result(self, error: str | None) -> None:
        async with self._state_lock:
            self._state.last_command_at_s = time.monotonic()
            self._state.last_error = error

    async def _finish_interrupted(self, turn: TurnState) -> ExecutionResult:
        turn.status = "interrupted"
        turn.interrupted = True
        turn.finished_at_s = time.monotonic()
        async with self._state_lock:
            self._state.busy = False
            self._state.current_turn = None
            self._state.last_turn = self._clone_turn(turn)
        return ExecutionResult(
            ok=False,
            interrupted=True,
            target_label=turn.target_label,
            motions=list(turn.motions),
            outcome="pending",
        )

    async def _finish_completed(
        self, turn: TurnState, *, outcome: ExecutionOutcome
    ) -> ExecutionResult:
        turn.status = "completed"
        turn.outcome = outcome
        turn.finished_at_s = time.monotonic()
        async with self._state_lock:
            self._state.busy = False
            self._state.current_turn = None
            self._state.last_turn = self._clone_turn(turn)
        return ExecutionResult(
            ok=True,
            interrupted=False,
            target_label=turn.target_label,
            motions=list(turn.motions),
            outcome=turn.outcome,
        )

    async def _finish_error(self, turn: TurnState, *, error: str) -> ExecutionResult:
        turn.status = "error"
        turn.error = error
        turn.finished_at_s = time.monotonic()
        async with self._state_lock:
            self._state.busy = False
            self._state.current_turn = None
            self._state.last_turn = self._clone_turn(turn)
            self._state.last_error = error
        return ExecutionResult(
            ok=False,
            interrupted=False,
            target_label=turn.target_label,
            motions=list(turn.motions),
            outcome="pending",
            error=error,
        )

    def _clone_turn(self, turn: TurnState | None) -> TurnState | None:
        if turn is None:
            return None
        return replace(turn, motions=list(turn.motions))


class _LegacyBackedExecutionController(_BaseExecutionController):
    def __init__(self, config: ClawControllerConfig, *, mode: str, sim: bool) -> None:
        super().__init__(config, mode=mode)
        self._legacy = LegacyClawController(
            port=config.serial_port,
            baud=config.serial_baud,
            sim=sim,
        )

    async def start(self) -> None:
        await self._legacy.start()
        await self._set_connected(connected=True, error=None)

    async def stop(self) -> None:
        await self._legacy.close()
        await self._set_connected(connected=False, error=None)

    async def get_machine_state(self) -> LegacyClawState:
        return self._legacy.state()

    async def move_axis(self, axis: str, degrees: float) -> LegacyCommandResult:
        result = await self._legacy.move_axis(axis, degrees)
        await self._record_legacy_result(result)
        return result

    async def lower_claw(self, degrees: float) -> LegacyCommandResult:
        result = await self._legacy.lower_claw(degrees)
        await self._record_legacy_result(result)
        return result

    async def raise_claw(self) -> LegacyCommandResult:
        result = await self._legacy.raise_claw()
        await self._record_legacy_result(result)
        return result

    async def open_claw(self, angle: int | None = None) -> LegacyCommandResult:
        result = await self._legacy.open_claw(angle)
        await self._record_legacy_result(result)
        return result

    async def close_claw(self, angle: int | None = None) -> LegacyCommandResult:
        result = await self._legacy.close_claw(angle)
        await self._record_legacy_result(result)
        return result

    async def home(self) -> LegacyCommandResult:
        result = await self._legacy.home()
        await self._record_legacy_result(result)
        return result

    async def home_z(self) -> LegacyCommandResult:
        result = await self._legacy.home_z()
        await self._record_legacy_result(result)
        return result

    async def halt(self) -> LegacyCommandResult:
        result = await self._legacy.halt()
        await self._record_legacy_result(result)
        return result

    async def reset_emergency(self) -> LegacyCommandResult:
        result = await self._legacy.reset_emergency()
        await self._record_legacy_result(result)
        return result

    async def _run_motion(self, motion: MotionDirection) -> str | None:
        if motion == "left":
            result = await self._legacy.move_axis("y", -self._config.y_move_degrees)
        elif motion == "right":
            result = await self._legacy.move_axis("y", self._config.y_move_degrees)
        elif motion == "forward":
            result = await self._legacy.move_axis("x", self._config.x_move_degrees)
        elif motion == "back":
            result = await self._legacy.move_axis("x", -self._config.x_move_degrees)
        elif motion == "down":
            result = await self._legacy.lower_claw(self._config.z_down_degrees)
        elif self._config.use_raise_for_up:
            result = await self._legacy.raise_claw()
        else:
            result = await self._legacy.move_axis("z", -self._config.z_up_degrees)

        await self._record_legacy_result(result)
        return None if result.ok else _format_command_error(result)

    async def _record_legacy_result(self, result: LegacyCommandResult) -> None:
        await self._record_command_result(
            error=None if result.ok else _format_command_error(result)
        )


class NoopClawControllerBackend(_LegacyBackedExecutionController):
    def __init__(self, config: ClawControllerConfig) -> None:
        super().__init__(config, mode="sim", sim=True)

    async def set_start_error(self, error: str | None) -> None:
        await self._set_last_error(error)


class LegacyProtocolClawControllerBackend(_LegacyBackedExecutionController):
    def __init__(self, config: ClawControllerConfig) -> None:
        super().__init__(config, mode="serial", sim=False)

    async def start(self) -> None:
        await self._legacy.start()
        is_sim = bool(getattr(self._legacy, "_sim", False))
        if is_sim:
            await self._legacy.close()
            raise RuntimeError(
                "legacy protocol backend could not establish serial transport"
            )
        await self._set_connected(connected=True, error=None)


class ClawController:
    """Facade that exposes one interface and selects backend implementation."""

    def __init__(self, config: ClawControllerConfig) -> None:
        self._config = config
        self._noop_backend = NoopClawControllerBackend(config)
        self._protocol_backend = LegacyProtocolClawControllerBackend(config)
        self._active_backend: ClawControllerBackend | None = None

    async def start(self) -> None:
        if self._active_backend is not None:
            return

        if self._config.mode == "sim":
            self._active_backend = self._noop_backend
            await self._active_backend.start()
            return

        if self._config.mode == "serial":
            self._active_backend = self._protocol_backend
            await self._active_backend.start()
            return

        # auto mode: prefer protocol backend, fallback to noop backend.
        try:
            await self._protocol_backend.start()
            self._active_backend = self._protocol_backend
        except Exception as exc:  # noqa: BLE001
            await self._noop_backend.start()
            await self._noop_backend.set_start_error(str(exc))
            self._active_backend = self._noop_backend

    async def stop(self) -> None:
        if self._active_backend is None:
            return
        await self._active_backend.stop()
        self._active_backend = None

    async def snapshot(self) -> ControllerState:
        backend = self._require_backend()
        return await backend.snapshot()

    def estimate_duration_s(self, motions: list[MotionDirection]) -> float:
        if self._active_backend is None:
            return self._noop_backend.estimate_duration_s(motions)
        return self._active_backend.estimate_duration_s(motions)

    async def execute_plan(
        self,
        *,
        target_label: str,
        motions: list[MotionDirection],
        should_interrupt: InterruptChecker,
        on_event: EventEmitter,
    ) -> ExecutionResult:
        backend = self._require_backend()
        return await backend.execute_plan(
            target_label=target_label,
            motions=motions,
            should_interrupt=should_interrupt,
            on_event=on_event,
        )

    async def get_state(self) -> dict[str, Any]:
        backend = self._require_backend()
        state = await backend.get_machine_state()
        return state.model_dump()

    async def move_axis(self, axis: str, degrees: float) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.move_axis(axis, degrees)
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def lower_claw(self, degrees: float) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.lower_claw(degrees)
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def raise_claw(self) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.raise_claw()
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def open_claw(self, angle: int | None = None) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.open_claw(angle)
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def close_claw(self, angle: int | None = None) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.close_claw(angle)
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def home(self) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.home()
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def home_z(self) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.home_z()
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def halt(self) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.halt()
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    async def reset_emergency(self) -> dict[str, Any]:
        backend = self._require_backend()
        result = await backend.reset_emergency()
        state = await backend.get_machine_state()
        return _result_to_dict(result, state=state)

    def _require_backend(self) -> ClawControllerBackend:
        if self._active_backend is None:
            raise RuntimeError("claw controller is not started")
        return self._active_backend
