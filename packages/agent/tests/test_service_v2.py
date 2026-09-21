"""Hardware-free wire replay through the installed Agents SDK and v2 service.

Only the socket, controller and speaker sink are replaced. SDK parsing, tool
dispatch, response sequencing, and application background tasks are real.
"""

import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from realtime_service.service_v2 import RealtimeClawVoiceServiceV2


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.send_error = None
        self.closed = False
        self.created_count = 0
        self.metadata = {}

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        if isinstance(event, Exception):
            raise event
        return json.dumps(event)

    async def send(self, message):
        if self.send_error:
            raise self.send_error
        self.sent.append(json.loads(message))

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)

    def receive(self, event_type, **fields):
        self.incoming.put_nowait({"type": event_type, "event_id": "test", **fields})

    def created(self, response_id):
        requests = [event for event in self.sent if event["type"] == "response.create"]
        metadata = requests[self.created_count].get("response", {}).get("metadata", {})
        self.created_count += 1
        self.metadata[response_id] = metadata
        self.receive(
            "response.created",
            response={"id": response_id, "status": "in_progress", "metadata": metadata},
        )

    def done(self, response_id, *, output=(), status="completed", status_details=None):
        self.receive(
            "response.done",
            response={
                "id": response_id,
                "status": status,
                "status_details": status_details,
                "output": list(output),
                "metadata": self.metadata.get(response_id, {}),
            },
        )

    def audio(self, response_id):
        self.receive(
            "response.output_audio.delta",
            response_id=response_id,
            item_id=f"audio_{response_id}",
            content_index=0,
            output_index=0,
            delta=base64.b64encode(b"\x00\x00" * 240).decode(),
        )

    def tool(self, response_id, call_id, name="open_claw", arguments="{}"):
        item = {
            "type": "function_call",
            "id": f"item_{call_id}",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
        self.receive(
            "response.output_item.done",
            response_id=response_id,
            output_index=0,
            item=item,
        )
        return item


async def settle():
    # Let all real SDK/session/tool tasks run without wall-clock sleeps.
    for _ in range(40):
        await asyncio.sleep(0)


class ServiceReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sockets = []
        self.connect_failures = []
        self.hanging_connect_attempts = 0
        self.connect_attempts = 0

        async def connect(**kwargs):
            self.connect_attempts += 1
            if self.hanging_connect_attempts:
                self.hanging_connect_attempts -= 1
                await asyncio.Event().wait()
            if self.connect_failures:
                raise self.connect_failures.pop(0)
            socket = FakeSocket()
            self.sockets.append(socket)
            return socket

        self.controller = AsyncMock()
        self.controller.get_state.return_value = {
            "fsm_state": "IDLE",
            "z": 0,
            "z_homed": True,
        }
        self.controller.open_claw.return_value = {"ok": True}
        self.controller.close_claw.return_value = {"ok": True}
        self.audio = AsyncMock()
        self.display = AsyncMock()
        for patcher in (
            patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test-placeholder"}),
            patch(
                "realtime_service.service_v2.ClawController",
                return_value=self.controller,
            ),
            patch("realtime_service.service_v2.ClawControllerConfig.from_env"),
            patch(
                "realtime_service.service_v2.LocalAudioOutput", return_value=self.audio
            ),
            patch(
                "agents.realtime.openai_realtime.OpenAIRealtimeWebSocketModel._create_websocket_connection",
                side_effect=connect,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = RealtimeClawVoiceServiceV2(self.display)
        await self.service.start()
        self.addAsyncCleanup(self.service.stop)

    async def submit(self, text="open the claw"):
        await self.service.submit_raw_text(text)
        await settle()
        return self.sockets[-1]

    async def test_tool_continuation_keeps_turn_alive_and_plays_audio(self):
        socket = await self.submit()
        socket.created("r1")
        item = socket.tool("r1", "c1")
        socket.done("r1", output=[item])
        await settle()
        self.controller.open_claw.assert_awaited_once()
        socket.created("r2")
        socket.audio("r2")
        socket.done("r2")
        await settle()
        self.audio.write.assert_awaited_once()
        self.assertIsNone(self.service._coordinator.current)

    async def test_parallel_tool_results_create_only_one_continuation(self):
        socket = await self.submit("open and close")
        socket.created("r1")
        first = socket.tool("r1", "c1")
        # Let the first result arrive before the server finishes streaming tools.
        await settle()
        second = socket.tool("r1", "c2", "close_claw")
        socket.done("r1", output=[first, second])
        await settle()
        socket.created("r2")
        socket.audio("r2")
        socket.done("r2")
        await settle()
        requests = [
            event for event in socket.sent if event["type"] == "response.create"
        ]
        self.assertEqual(
            len(requests), 2, "surplus tool continuations leak into subsequent turns"
        )

    async def test_send_failure_triggers_one_immediate_reconnect(self):
        self.sockets[0].send_error = ConnectionResetError("injected send failure")
        await self.submit("first")
        async with asyncio.timeout(1):
            while len(self.sockets) < 2 or self.service._session is None:
                await asyncio.sleep(0)
        await settle()
        self.assertFalse(
            self.service._coordinator._task.done(), "command worker died permanently"
        )
        self.assertEqual(len(self.sockets), 2, "send failure created a reconnect storm")
        await self.submit("second")
        self.assertTrue(
            any("second" in json.dumps(event) for event in self.sockets[-1].sent)
        )

    async def test_receive_failure_triggers_one_immediate_reconnect(self):
        socket = await self.submit()
        socket.incoming.put_nowait(ConnectionResetError("injected receive failure"))
        async with asyncio.timeout(1):
            while len(self.sockets) < 2 or self.service._session is None:
                await asyncio.sleep(0)
        await settle()
        self.assertEqual(len(self.sockets), 2, "failure created a reconnect storm")
        await self.submit("second")
        self.assertFalse(self.service._events.done())

    async def test_clean_transport_close_reconnects_before_later_input(self):
        socket = await self.submit()
        await socket.close()
        async with asyncio.timeout(1):
            while len(self.sockets) < 2 or self.service._session is None:
                await asyncio.sleep(0)
        self.assertEqual(
            len(self.sockets), 2, "clean transport close did not reconnect immediately"
        )
        await self.submit("second")
        self.assertTrue(
            any("second" in json.dumps(event) for event in self.sockets[-1].sent)
        )
        replacement = self.sockets[-1]
        replacement.created("r2")
        replacement.audio("r2")
        replacement.done("r2")
        await settle()
        self.audio.write.assert_awaited_once()
        self.assertIsNone(self.service._coordinator.current)
        self.assertFalse(self.service._events.done())

    async def test_reconnect_retries_transient_connection_failure(self):
        socket = await self.submit()
        self.service._reconnect_initial_delay_s = 0.001
        self.connect_failures.append(ConnectionResetError("reconnect failed once"))
        await socket.close()
        async with asyncio.timeout(1):
            while len(self.sockets) < 2 or self.service._session is None:
                await asyncio.sleep(0)
        self.assertEqual(len(self.connect_failures), 0)
        self.assertFalse(self.service._events.done())

    async def test_reconnect_times_out_hung_connection_attempt(self):
        socket = await self.submit()
        self.service._connect_timeout_s = 0.01
        self.service._reconnect_initial_delay_s = 0.001
        self.hanging_connect_attempts = 1
        await socket.close()
        async with asyncio.timeout(1):
            while len(self.sockets) < 2 or self.service._session is None:
                await asyncio.sleep(0)
        self.assertGreaterEqual(self.connect_attempts, 3)
        self.assertFalse(self.service._events.done())

    async def test_stop_cancels_reconnect_backoff(self):
        socket = await self.submit()
        self.service._reconnect_initial_delay_s = 0.01
        self.service._reconnect_max_delay_s = 0.01
        self.connect_failures.extend(
            ConnectionResetError(f"reconnect failure {n}") for n in range(20)
        )
        await socket.close()
        async with asyncio.timeout(1):
            while self.connect_attempts < 2:
                await asyncio.sleep(0)
        await self.service.stop()
        attempts_after_stop = self.connect_attempts
        await asyncio.sleep(0.03)
        self.assertEqual(self.connect_attempts, attempts_after_stop)
        self.assertIsNone(self.service._session)

    async def test_stop_racing_transport_close_finishes_cleanup(self):
        socket = await self.submit()
        session = self.service._session
        await asyncio.wait_for(
            asyncio.gather(socket.close(), self.service.stop()), timeout=2.0
        )
        self.assertIsNone(self.service._session)
        self.assertIsNone(session.model._websocket_task)
        await settle()
        self.assertEqual(len(self.sockets), 1, "shutdown unexpectedly reconnected")

    async def test_speaker_failure_does_not_kill_event_bridge(self):
        socket = await self.submit()
        self.audio.write.side_effect = RuntimeError("injected speaker failure")
        socket.created("r1")
        socket.audio("r1")
        socket.done("r1")
        await settle()
        self.assertFalse(
            self.service._events.done(), "speaker failure killed the SDK event consumer"
        )
        await self.submit("second")
        socket.created("r2")
        socket.done("r2")
        await settle()
        self.assertIsNone(self.service._coordinator.current)

    async def test_old_response_done_does_not_finish_replacement_turn(self):
        socket = await self.submit("first")
        socket.created("r1")
        await settle()
        await self.submit("second")
        socket.done("r1", status="cancelled")
        await settle()
        socket.created("r2")
        socket.audio("r2")
        socket.done("r2")
        await settle()
        self.audio.write.assert_awaited_once()

    async def test_replacement_before_first_response_ack_keeps_its_turn(self):
        socket = await self.submit("first")
        await self.submit("second")
        socket.created("r1")
        socket.done("r1", status="cancelled")
        await settle()
        self.assertIsNotNone(self.service._coordinator.current)
        socket.created("r2")
        socket.audio("r2")
        socket.done("r2")
        await settle()
        self.audio.write.assert_awaited_once()

    async def test_session_failure_does_not_release_in_flight_hardware_lock(self):
        release = asyncio.Event()
        self.addCleanup(release.set)

        async def blocked_open(angle):
            await release.wait()
            return {"ok": True}

        self.controller.open_claw.side_effect = blocked_open
        first_socket = await self.submit("open")
        first_socket.created("r1")
        first = first_socket.tool("r1", "c1")
        first_socket.done("r1", output=[first])
        await settle()
        self.controller.open_claw.assert_awaited_once()

        first_socket.incoming.put_nowait(ConnectionResetError("transport failed"))
        await settle()
        second_socket = await self.submit("close")
        second_socket.created("r2")
        second = second_socket.tool("r2", "c2", "close_claw")
        second_socket.done("r2", output=[second])
        await settle()
        self.controller.close_claw.assert_not_awaited()

        release.set()
        await settle()
        self.controller.close_claw.assert_awaited_once()

    async def test_old_tool_waiting_for_controller_cannot_run_in_new_turn(self):
        release = asyncio.Event()

        async def blocked_open(angle):
            await release.wait()
            return {"ok": True}

        self.controller.open_claw.side_effect = blocked_open
        socket = await self.submit("open then close")
        socket.created("r1")
        first = socket.tool("r1", "c1")
        second = socket.tool("r1", "c2", "close_claw")
        socket.done("r1", output=[first, second])
        await settle()
        self.controller.open_claw.assert_awaited_once()
        await self.submit("replacement")
        self.display.broadcast.reset_mock()
        release.set()
        await settle()
        self.controller.close_claw.assert_not_awaited()
        self.assertFalse(
            any(
                call.args[0].get("status") == "complete"
                for call in self.display.broadcast.await_args_list
            ),
            "old tool completion changed the replacement display",
        )

    async def test_speaker_timeout_leaves_commands_usable(self):
        blocked = asyncio.Event()

        async def hang(*args, **kwargs):
            await blocked.wait()

        self.audio.write.side_effect = hang
        self.service._audio_timeout = 0.01
        socket = await self.submit()
        socket.created("r1")
        socket.audio("r1")
        socket.done("r1")
        async with asyncio.timeout(1):
            while self.service._coordinator.current is not None:
                await asyncio.sleep(0.001)
        self.assertFalse(self.service._events.done())

    async def test_recoverable_cancel_error_preserves_session(self):
        socket = await self.submit()
        socket.receive(
            "error",
            error={
                "type": "invalid_request_error",
                "code": "response_cancel_not_active",
                "message": "No active response",
            },
        )
        await settle()
        self.assertIsNotNone(self.service._session)
        self.assertFalse(self.service._events.done())
        socket.created("r1")
        socket.audio("r1")
        socket.done("r1")
        await settle()
        self.audio.write.assert_awaited_once()

    async def test_failed_response_reports_error_and_allows_next_turn(self):
        socket = await self.submit()
        socket.created("r1")
        socket.done(
            "r1",
            status="failed",
            status_details={
                "type": "failed",
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Injected rate limit",
                },
            },
        )
        await settle()
        self.assertIsNone(self.service._coordinator.current)
        self.display.broadcast.assert_any_await({"type": "state", "state": "error"})
        await self.submit("second")
        socket.created("r2")
        socket.audio("r2")
        socket.done("r2")
        await settle()
        self.audio.write.assert_awaited_once()

    async def test_submit_racing_stop_restarts_after_controller_teardown(self):
        stop_entered = asyncio.Event()
        allow_stop = asyncio.Event()
        self.addCleanup(allow_stop.set)

        async def blocked_stop():
            stop_entered.set()
            await allow_stop.wait()

        self.controller.stop.side_effect = blocked_stop
        stop_task = asyncio.create_task(self.service.stop())
        await asyncio.wait_for(stop_entered.wait(), timeout=1.0)
        submit_task = asyncio.create_task(self.service.submit_raw_text("replacement"))
        await settle()
        self.assertEqual(len(self.sockets), 1)

        allow_stop.set()
        await asyncio.wait_for(asyncio.gather(stop_task, submit_task), timeout=2.0)
        await settle()
        self.assertEqual(len(self.sockets), 2)
        self.assertEqual(self.controller.start.await_count, 2)
        self.assertIsNotNone(self.service._session)
        self.assertFalse(self.service._events.done())

    async def test_concurrent_start_creates_one_connection(self):
        await self.service.stop()
        self.service = RealtimeClawVoiceServiceV2(self.display)
        self.addAsyncCleanup(self.service.stop)
        before = len(self.sockets)
        await asyncio.gather(*(self.service.start() for _ in range(10)))
        self.assertEqual(len(self.sockets), before + 1)

    async def test_multiple_tools_and_repeated_turns_keep_working(self):
        for n in range(50):
            socket = await self.submit(f"command {n}")
            socket.created(f"r{n}_a")
            first = socket.tool(f"r{n}_a", f"c{n}_a")
            socket.done(f"r{n}_a", output=[first])
            await settle()
            socket.created(f"r{n}_b")
            second = socket.tool(f"r{n}_b", f"c{n}_b", "close_claw")
            socket.done(f"r{n}_b", output=[second])
            await settle()
            socket.created(f"r{n}_c")
            socket.audio(f"r{n}_c")
            socket.done(f"r{n}_c")
            await settle()
            self.assertIsNone(self.service._coordinator.current)
        self.assertEqual(self.controller.open_claw.await_count, 50)
        self.assertEqual(self.controller.close_claw.await_count, 50)
        self.assertEqual(self.audio.write.await_count, 50)
        self.assertEqual(len(self.sockets), 1)
