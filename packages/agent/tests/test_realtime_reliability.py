import asyncio
import json
import unittest

from common.broadcaster import DisplayBroadcaster
from realtime_service.turn_coordinator import TurnCoordinator


async def wait_until(predicate, *, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class TurnCoordinatorReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_interrupt_does_not_clear_replacement_turn(self):
        cancel_gates = [asyncio.Event(), asyncio.Event()]
        cancel_count = 0
        started = []

        async def cancel_response():
            nonlocal cancel_count
            gate = cancel_gates[cancel_count]
            cancel_count += 1
            await gate.wait()

        async def start_response(turn):
            started.append(turn)

        coordinator = TurnCoordinator(
            cancel_response=cancel_response,
            start_response=start_response,
        )
        await coordinator.start()
        self.addAsyncCleanup(coordinator.stop)

        coordinator.submit("first")
        await wait_until(lambda: len(started) == 1)
        late_interrupt = asyncio.create_task(coordinator.interrupt())
        await wait_until(lambda: cancel_count == 1)

        coordinator.submit("second")
        await wait_until(lambda: cancel_count == 2)
        cancel_gates[1].set()
        await wait_until(lambda: len(started) == 2)
        self.assertEqual(coordinator.current.text, "second")

        cancel_gates[0].set()
        await late_interrupt
        self.assertEqual(coordinator.current.text, "second")


class BlockingClient:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def send(self, message):
        del message
        self.started.set()
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


class RecordingClient:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class DisplayBroadcasterReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_client_does_not_block_healthy_client(self):
        broadcaster = DisplayBroadcaster(send_timeout_s=0.01)
        blocked = BlockingClient()
        healthy = RecordingClient()
        await broadcaster.add_client(blocked)
        await broadcaster.add_client(healthy)

        async with asyncio.timeout(1):
            await broadcaster.broadcast({"type": "state", "state": "thinking"})

        self.assertEqual(healthy.messages, [{"type": "state", "state": "thinking"}])
        self.assertNotIn(blocked, await broadcaster.snapshot_clients())
        self.assertTrue(blocked.closed)
        self.assertIn(healthy, await broadcaster.snapshot_clients())
