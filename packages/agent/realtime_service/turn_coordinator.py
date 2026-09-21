from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable


class TurnPhase(StrEnum):
    IDLE = "idle"
    RESPONDING = "responding"
    INTERRUPTING = "interrupting"


@dataclass(frozen=True)
class Turn:
    id: int
    text: str
    phase: TurnPhase


class TurnCoordinator:
    """Single owner of latest-text-wins turn state.

    Callbacks do I/O; this class never holds a mutex while they run. A generation
    number gives every downstream event a cheap, explicit eligibility check.
    """

    def __init__(
        self,
        *,
        cancel_response: Callable[[], Awaitable[None]],
        start_response: Callable[[Turn], Awaitable[None]],
    ) -> None:
        self._cancel_response = cancel_response
        self._start_response = start_response
        self._pending: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._turn: Turn | None = None
        self._next_id = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._turn = None

    def submit(self, text: str) -> None:
        if self._pending.full():
            self._pending.get_nowait()
        self._pending.put_nowait(text)

    async def interrupt(self) -> None:
        if self._turn is None:
            return
        interrupted_id = self._turn.id
        self._turn = Turn(interrupted_id, self._turn.text, TurnPhase.INTERRUPTING)
        await self._cancel_response()
        if (
            self._turn is not None
            and self._turn.id == interrupted_id
            and self._turn.phase == TurnPhase.INTERRUPTING
        ):
            self._turn = None

    def is_current(self, turn_id: int) -> bool:
        return (
            self._turn is not None
            and self._turn.id == turn_id
            and self._turn.phase == TurnPhase.RESPONDING
        )

    @property
    def current(self) -> Turn | None:
        return self._turn

    async def finish(self, turn_id: int) -> bool:
        if not self.is_current(turn_id):
            return False
        self._turn = None
        return True

    async def _run(self) -> None:
        while True:
            text = await self._pending.get()
            await self.interrupt()
            self._next_id += 1
            turn = Turn(self._next_id, text, TurnPhase.RESPONDING)
            self._turn = turn
            await self._start_response(turn)
