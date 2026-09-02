from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from agents.decorators import function_tool

from common.claw_controller import ClawController
from .prompt import EMOTION_MAP

Emit = Callable[[dict[str, Any]], Awaitable[None]]
IsCurrent = Callable[[], bool]


class ClawToolbox:
    """The only module allowed to issue physical claw commands."""

    def __init__(
        self,
        controller: ClawController,
        emit: Emit,
        is_current: IsCurrent,
    ) -> None:
        self._controller = controller
        self._emit = emit
        self._is_current = is_current
        self._hardware_lock = asyncio.Lock()
        z_degrees_per_foot = float(os.getenv("CLAW_Z_DEGREES_PER_FOOT", "360"))
        self._default_lower = float(
            os.getenv("CLAW_DEFAULT_LOWER_DEGREES", str(z_degrees_per_foot * 3))
        )

    async def move_axis(
        self, axis: Literal["x", "y", "z"], degrees: float
    ) -> dict[str, Any]:
        """Move one axis by signed degrees. axis: x/y/z. X positive=forward, Y positive=right, Z positive=down."""
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        direction = {
            "x": "forward" if degrees > 0 else "back",
            "y": "right" if degrees > 0 else "left",
            "z": "down" if degrees > 0 else "up",
        }[axis]
        await self._motion(direction, 0.55 if direction == "down" else 0.8)
        return await self._move_axis(axis, degrees)

    async def _move_axis(self, axis: str, degrees: float) -> dict[str, Any]:
        async def command() -> dict[str, Any]:
            return await self._controller.move_axis(axis, degrees)

        return await self._physical(command)

    async def open_claw(self) -> dict[str, Any]:
        """Open claw servo. Optional angle 25-90."""
        return await self._physical(self._open_claw)

    async def _open_claw(self) -> dict[str, Any]:
        return await self._controller.open_claw(90)

    async def lower_claw(
        self, degrees: Annotated[float, Field(ge=0)] | None = None
    ) -> dict[str, Any]:
        """Lower claw by positive degrees. If omitted, uses default depth."""
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        amount = abs(self._default_lower if degrees is None else degrees)
        await self._emit({"type": "state", "state": "dropping"})
        await self._emit({"type": "effect", "effect": "dropStarted"})
        await self._emit({"type": "claw_motion", "direction": "down", "speed": 0.55})

        async def command() -> dict[str, Any]:
            return await self._controller.lower_claw(amount)

        return await self._physical(command)

    async def raise_claw(self) -> dict[str, Any]:
        """Retract claw to top limit (Z=0 reference)."""
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        await self._motion("up", 0.7)
        return await self._physical(self._controller.raise_claw)

    async def close_claw(self) -> dict[str, Any]:
        """Close claw servo. Optional angle 25-90."""
        return await self._physical(self._close_claw)

    async def _close_claw(self) -> dict[str, Any]:
        return await self._controller.close_claw(25)

    async def home_z(self) -> dict[str, Any]:
        """Home Z only by driving upward to top limit switch."""
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        await self._emit({"type": "state", "state": "moving"})
        return await self._physical(self._controller.home_z)

    async def home(self) -> dict[str, Any]:
        """Run full homing and park/dropoff sequence."""
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        await self._emit({"type": "state", "state": "moving"})
        return await self._physical(self._controller.home)

    async def get_state(self) -> dict[str, Any]:
        """Get current machine state snapshot."""
        return {"ok": True, "state": await self._controller.get_state()}

    async def halt(self) -> dict[str, Any]:
        """Emergency stop. Halts motion immediately."""
        return await self._physical(self._controller.halt)

    async def reset_emergency(self) -> dict[str, Any]:
        """Clear emergency-stop latch."""
        return await self._physical(self._controller.reset_emergency)

    async def set_expression(
        self,
        mood: Literal[
            "calm",
            "blink",
            "wink",
            "suspicious",
            "excited",
            "confused",
            "thinking",
            "nervous",
            "stressed",
            "disappointed",
            "surprised",
            "love",
        ],
        emotion: str | None = None,
    ) -> dict[str, Any]:
        """Set frontend expression mood for the current turn."""
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        normalized = EMOTION_MAP.get(mood.lower(), mood)
        await self._emit(
            {"type": "emotion", "mood": normalized, "emotion": emotion or mood}
        )
        return {"ok": True, "mood": normalized, "emotion": emotion or mood}

    async def _physical(
        self, command: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        if not self._is_current():
            return {"ok": False, "error": "turn_interrupted"}
        await self._emit(
            {"type": "agent_step", "step": "claw_execute", "status": "active"}
        )
        async with self._hardware_lock:
            if not self._is_current():
                return {"ok": False, "error": "turn_interrupted"}
            result = await command()
        status = "complete" if result.get("ok") else "error"
        await self._emit(
            {"type": "agent_step", "step": "claw_execute", "status": status}
        )
        await self._emit(
            {"type": "agent_step", "step": "result_evaluate", "status": status}
        )
        return result

    async def _motion(self, direction: str, speed: float) -> None:
        await self._emit(
            {"type": "state", "state": "dropping" if direction == "down" else "moving"}
        )
        await self._emit(
            {"type": "claw_motion", "direction": direction, "speed": speed}
        )

    def attachments(self) -> list[Any]:
        return [
            function_tool(self.move_axis, strict_mode=False),
            function_tool(self.open_claw),
            function_tool(self.lower_claw, strict_mode=False),
            function_tool(self.raise_claw),
            function_tool(self.close_claw),
            function_tool(self.home_z),
            function_tool(self.home),
            function_tool(self.get_state),
            function_tool(self.halt),
            function_tool(self.reset_emergency),
            function_tool(self.set_expression, strict_mode=False),
        ]
