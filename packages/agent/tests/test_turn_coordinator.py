import asyncio
import unittest

from realtime_service.turn_coordinator import TurnCoordinator


class TurnCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_text_replaces_pending_text(self) -> None:
        started: list[str] = []
        coordinator = TurnCoordinator(
            cancel_response=lambda: asyncio.sleep(0),
            start_response=lambda turn: _record(started, turn.text),
        )
        await coordinator.start()
        coordinator.submit("first")
        coordinator.submit("second")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(started, ["second"])
        await coordinator.stop()

    async def test_interrupt_invalidates_active_turn_before_cancel(self) -> None:
        started: list[int] = []
        cancelled: list[int] = []
        coordinator: TurnCoordinator

        async def cancel() -> None:
            turn = coordinator.current
            cancelled.append(turn.id if turn else -1)

        coordinator = TurnCoordinator(
            cancel_response=cancel,
            start_response=lambda turn: _record(started, turn.id),
        )
        await coordinator.start()
        coordinator.submit("move")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        turn = coordinator.current
        assert turn is not None
        await coordinator.interrupt()
        self.assertFalse(coordinator.is_current(turn.id))
        self.assertEqual(cancelled, [turn.id])
        await coordinator.stop()


async def _record(items: list[object], value: object) -> None:
    items.append(value)
