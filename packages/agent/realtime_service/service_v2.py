from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from agents.realtime import (
    RealtimeAgent,
    RealtimeRawModelEvent,
    RealtimeRunner,
    RealtimeSession,
    RealtimeToolEnd,
)

from common.audio_output import LocalAudioOutput
from common.broadcaster import DisplayBroadcaster
from common.claw_controller import ClawController, ClawControllerConfig
from .claw_tools import ClawToolbox
from .prompt import build_system_prompt
from .turn_coordinator import Turn, TurnCoordinator


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
        self._pending_tool_calls = 0
        self._agent_end_received = False
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
            tools=self._tools.attachments(),
        )

    async def start(self) -> None:
        if self._session is not None:
            return
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required")
        await self._controller.start()
        runner = RealtimeRunner(
            self._agent,
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
        self._session = await runner.run()
        await self._session.enter()
        await self._coordinator.start()
        self._events = asyncio.create_task(self._bridge_events())

    async def stop(self) -> None:
        await self._coordinator.stop()
        if self._events:
            self._events.cancel()
            await asyncio.gather(self._events, return_exceptions=True)
            self._events = None
        await self._audio.close()
        if self._session:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        await self._controller.stop()

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
        self._pending_tool_calls = 0
        self._agent_end_received = False
        await self._emit({"type": "transcript", "text": turn.text, "isFinal": True})
        await self._emit({"type": "state", "state": "thinking"})
        await self._session_or_die().send_message(
            await self._machine_context(turn.text)
        )

    async def _cancel_response(self) -> None:
        await self._audio.close()
        if self._session:
            with contextlib.suppress(Exception):
                await self._session.interrupt()
        await self._emit({"type": "emotion_clear"})

    async def _bridge_events(self) -> None:
        async for event in self._session_or_die():
            if event.type == "audio" and self._has_active_turn():
                await self._audio.write(
                    event.audio.data, sample_rate=24000, dtype="int16"
                )
            elif event.type == "audio_interrupted":
                await self._audio.close()
            elif (
                isinstance(event, RealtimeRawModelEvent)
                and event.data.type == "function_call"
            ):
                self._pending_tool_calls += 1
            elif isinstance(event, RealtimeToolEnd):
                self._pending_tool_calls = max(0, self._pending_tool_calls - 1)
                await self._finish_turn_after_tools()
            elif event.type == "agent_end":
                self._agent_end_received = True
                await self._finish_turn_after_tools()
            elif event.type == "error":
                await self._reset_failed_session()

    async def _finish_turn_after_tools(self) -> None:
        if not self._agent_end_received or self._pending_tool_calls:
            return
        turn = self._coordinator.current
        if turn is not None and await self._coordinator.finish(turn.id):
            await self._emit({"type": "emotion_clear"})
            await self._emit({"type": "state", "state": "attract"})

    async def _reset_failed_session(self) -> None:
        """Return the display to idle; the next text input opens a fresh session."""
        await self._coordinator.interrupt()
        await self._audio.close()
        await self._emit({"type": "state", "state": "error"})
        await self._emit({"type": "effect", "effect": "error"})
        await self._emit({"type": "emotion_clear"})
        await self._emit({"type": "state", "state": "attract"})
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    def _has_active_turn(self) -> bool:
        turn = self._coordinator.current
        return turn is not None and self._coordinator.is_current(turn.id)

    async def _machine_context(self, text: str) -> str:
        state = await self._controller.get_state()
        return f"[machine status: fsm={state.get('fsm_state')}, Z={state.get('z')}, Z_homed={state.get('z_homed')}]\n{text}"

    async def _emit(self, event: dict[str, Any]) -> None:
        await self._broadcaster.broadcast(event)

    def _session_or_die(self) -> RealtimeSession:
        if self._session is None:
            raise RuntimeError("Realtime session is not running")
        return self._session
