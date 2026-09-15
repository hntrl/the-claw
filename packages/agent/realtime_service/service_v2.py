from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextvars import ContextVar
from typing import Any

from agents.realtime import (
    RealtimeAgent,
    RealtimeRawModelEvent,
    RealtimeRunner,
    RealtimeSession,
)

from common.audio_output import LocalAudioOutput
from common.broadcaster import DisplayBroadcaster
from common.claw_controller import ClawController, ClawControllerConfig
from .claw_tools import ClawToolbox
from .prompt import build_system_prompt
from .turn_coordinator import Turn, TurnCoordinator
from .turn_model import TurnAwareRealtimeModel

logger = logging.getLogger(__name__)


class RealtimeClawVoiceServiceV2:
    """Composition root for a text-input OpenAI Realtime claw agent."""

    def __init__(
        self, broadcaster: DisplayBroadcaster, *, success_rate: float = 0.68
    ) -> None:
        self._broadcaster = broadcaster
        self._controller = ClawController(
            ClawControllerConfig.from_env(success_rate=success_rate)
        )
        self._audio = LocalAudioOutput()
        self._session: RealtimeSession | None = None
        self._events: asyncio.Task[None] | None = None
        self._session_lock = asyncio.Lock()
        self._controller_started = False
        self._stopping = False
        self._response_turns: dict[str, int] = {}
        self._tool_turns: dict[str, int] = {}
        self._invoking_turn: ContextVar[int | None] = ContextVar(
            "claw_tool_turn", default=None
        )
        self._play_audio = os.getenv("AGENT_REALTIME_PLAY_AUDIO", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._audio_timeout = max(
            0.1, float(os.getenv("AGENT_REALTIME_AUDIO_WRITE_TIMEOUT_S", "2.0"))
        )
        self._coordinator = TurnCoordinator(
            cancel_response=self._cancel_response,
            start_response=self._start_turn,
        )
        self._tools = ClawToolbox(
            self._controller,
            self._emit,
            self._has_active_turn,
        )
        self._agent = RealtimeAgent(
            name="ClawPilot",
            instructions=build_system_prompt(),
            tools=[self._bind_tool(tool) for tool in self._tools.attachments()],
        )

    def _bind_tool(self, tool: Any) -> Any:
        invoke = tool.on_invoke_tool

        async def invoke_for_turn(context: Any, arguments: str) -> Any:
            turn_id = self._tool_turns.get(context.tool_call_id)
            if turn_id is None or not self._coordinator.is_current(turn_id):
                return {"ok": False, "error": "turn_interrupted"}
            token = self._invoking_turn.set(turn_id)
            try:
                return await invoke(context, arguments)
            finally:
                self._invoking_turn.reset(token)

        tool.on_invoke_tool = invoke_for_turn
        return tool

    async def on_event(self, event: Any) -> None:
        """Bind IDs on receipt, before SDK tool dispatch or queued speaker playback."""
        if event.type != "raw_server_event":
            return
        data = event.data
        if data.get("type") == "response.created":
            response = data["response"]
            turn_id = (response.get("metadata") or {}).get("claw_turn_id")
            if isinstance(turn_id, str) and turn_id.isdecimal():
                self._response_turns[response["id"]] = int(turn_id)
        elif data.get("type") == "response.output_item.done":
            item = data.get("item", {})
            turn_id = self._response_turns.get(data.get("response_id"))
            if item.get("type") == "function_call" and turn_id is not None:
                self._tool_turns[item["call_id"]] = turn_id

    async def start(self) -> None:
        async with self._session_lock:
            if self._session is not None:
                return
            await self._open_session()

    async def _open_session(self) -> None:
        self._stopping = False
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required")
        if not self._controller_started:
            await self._controller.start()
            self._controller_started = True
        runner = RealtimeRunner(
            self._agent,
            model=TurnAwareRealtimeModel(
                self._current_turn_id,
                lambda call_id: self._tool_turns.pop(call_id, None),
                self._coordinator.is_current,
            ),
            config={
                "model_settings": {
                    "model_name": os.getenv(
                        "OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"
                    ),
                    "output_modalities": ["audio"],
                    "audio": {
                        "output": {
                            "format": "pcm16",
                            "voice": os.getenv("OPENAI_REALTIME_VOICE", "marin"),
                        }
                    },
                    "tool_choice": "auto",
                }
            },
        )
        session = await runner.run()
        session.model.add_listener(self)
        try:
            await session.enter()
        except BaseException:
            session.model.remove_listener(self)
            await session.close()
            raise
        self._session = session
        self._response_turns.clear()
        self._tool_turns.clear()
        await self._coordinator.start()
        self._events = asyncio.create_task(self._bridge_events(session))

    async def stop(self) -> None:
        self._stopping = True
        await self._coordinator.stop()
        if self._events:
            self._events.cancel()
            await asyncio.gather(self._events, return_exceptions=True)
            self._events = None
        await self._close_audio()
        if self._session:
            self._session.model.remove_listener(self)
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        await self._controller.stop()
        self._controller_started = False

    async def submit_raw_text(self, text: str, *, source: str = "text") -> None:
        del source
        if not text.strip():
            return
        await self.start()
        self._coordinator.submit(text.strip())

    async def submit_utterance(self, text: str, *, source: str = "text") -> None:
        await self.submit_raw_text(text, source=source)

    async def request_interrupt(self) -> None:
        await self._coordinator.interrupt()

    async def _start_turn(self, turn: Turn) -> None:
        session = None
        try:
            # A queued command can reach us after the previous session failed.
            await self.start()
            session = self._session_or_die()
            await self._emit({"type": "transcript", "text": turn.text, "isFinal": True})
            await self._emit({"type": "state", "state": "thinking"})
            await session.send_message(await self._machine_context(turn.text))
        except Exception as exc:
            logger.error(
                "Realtime send failed turn_id=%s error_type=%s",
                turn.id,
                type(exc).__name__,
            )
            await self._reset_failed_session(session)

    async def _cancel_response(self) -> None:
        await self._close_audio()
        if self._session:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._session.interrupt(), timeout=2.0)
        await self._emit({"type": "emotion_clear"})

    async def _bridge_events(self, session: RealtimeSession) -> None:
        try:
            async for event in session:
                if self._session is not session:
                    return
                if (
                    isinstance(event, RealtimeRawModelEvent)
                    and event.data.type == "raw_server_event"
                ):
                    await self._handle_server_event(event.data.data)
                elif event.type == "audio" and self._response_is_current(
                    event.audio.response_id
                ):
                    await self._write_audio(event.audio.data)
                elif event.type == "error":
                    error = event.error
                    code = (
                        error.get("code")
                        if isinstance(error, dict)
                        else getattr(error, "code", None)
                    )
                    # Cancellation can race response.done; this does not invalidate the session.
                    if code == "response_cancel_not_active":
                        continue
                    logger.error(
                        "Realtime error code=%s error_type=%s",
                        code,
                        type(error).__name__,
                    )
                    return
        except Exception as exc:
            # The SDK raises transport failures from its iterator, not as error events.
            logger.error(
                "Realtime event stream failed error_type=%s", type(exc).__name__
            )
        finally:
            if not self._stopping:
                await self._reset_failed_session(session)

    def _response_is_current(self, response_id: str) -> bool:
        turn_id = self._response_turns.get(response_id)
        return turn_id is not None and self._coordinator.is_current(turn_id)

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        response = event.get("response", {})
        response_id = response.get("id")
        if event.get("type") == "response.done":
            turn_id = self._response_turns.pop(response_id, None)
            if turn_id is None or not self._coordinator.is_current(turn_id):
                return
            if response.get("status") in {"failed", "incomplete", "cancelled"}:
                if response.get("status") != "cancelled":
                    details = response.get("status_details") or {}
                    logger.error(
                        "Realtime response ended turn_id=%s status=%s code=%s",
                        turn_id,
                        response.get("status"),
                        (details.get("error") or {}).get("code"),
                    )
                    await self._emit({"type": "state", "state": "error"})
                    await self._emit({"type": "effect", "effect": "error"})
                await self._finish_turn(turn_id)
                return
            # agent_end means ONE response ended. Tool output automatically starts
            # another response; only a response without function calls ends the turn.
            if any(
                item.get("type") == "function_call"
                for item in response.get("output", [])
            ):
                return
            await self._finish_turn(turn_id)

    async def _finish_turn(self, turn_id: int) -> None:
        if await self._coordinator.finish(turn_id):
            await self._emit({"type": "emotion_clear"})
            await self._emit({"type": "state", "state": "attract"})

    async def _write_audio(self, data: bytes) -> None:
        if not self._play_audio:
            return
        try:
            await asyncio.wait_for(
                self._audio.write(data, sample_rate=24000, dtype="int16"),
                timeout=self._audio_timeout,
            )
        except Exception as exc:
            # Speaker failure must not stop consuming model/tool/connection events.
            self._play_audio = False
            logger.error("Speaker output disabled error_type=%s", type(exc).__name__)
            await self._close_audio()

    async def _close_audio(self) -> None:
        try:
            await asyncio.wait_for(self._audio.close(), timeout=self._audio_timeout)
        except Exception as exc:
            logger.warning("Speaker close failed error_type=%s", type(exc).__name__)

    async def _reset_failed_session(
        self, failed_session: RealtimeSession | None
    ) -> None:
        """Return the display to idle; the next text input opens a fresh session."""
        async with self._session_lock:
            if self._session is not failed_session:
                return
            await self._coordinator.interrupt()
            self._session = None
            self._response_turns.clear()
            self._tool_turns.clear()
            if failed_session is not None:
                failed_session.model.remove_listener(self)
                with contextlib.suppress(Exception):
                    await failed_session.close()
            await self._emit({"type": "state", "state": "error"})
            await self._emit({"type": "effect", "effect": "error"})
            await self._emit({"type": "emotion_clear"})
            await self._emit({"type": "state", "state": "attract"})

    def _has_active_turn(self) -> bool:
        invoking_turn = self._invoking_turn.get()
        if invoking_turn is not None:
            return self._coordinator.is_current(invoking_turn)
        turn = self._coordinator.current
        return turn is not None and self._coordinator.is_current(turn.id)

    def _current_turn_id(self) -> int | None:
        turn = self._coordinator.current
        return (
            turn.id
            if turn is not None and self._coordinator.is_current(turn.id)
            else None
        )

    async def _machine_context(self, text: str) -> str:
        state = await self._controller.get_state()
        return f"[machine status: fsm={state.get('fsm_state')}, Z={state.get('z')}, Z_homed={state.get('z_homed')}]\n{text}"

    async def _emit(self, event: dict[str, Any]) -> None:
        invoking_turn = self._invoking_turn.get()
        if invoking_turn is not None and not self._coordinator.is_current(
            invoking_turn
        ):
            return
        await self._broadcaster.broadcast(event)

    def _session_or_die(self) -> RealtimeSession:
        if self._session is None:
            raise RuntimeError("Realtime session is not running")
        return self._session
