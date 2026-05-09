from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Any

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from common.broadcaster import DisplayBroadcaster
from common.claw_controller import ClawController, ClawControllerConfig
from .frames import DisplayEventFrame, RawTextFrame, UtteranceFrame
from .processors import (
    AgentProcessor,
    CartesiaMarkupProcessor,
    DisplayEmitter,
    DisplayEventDispatchProcessor,
    SpeechToTextProcessor,
    TTSSpeakProcessor,
    VoiceStartProcessor,
)


class ExecutionControl:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._interrupt_event = asyncio.Event()
        self._executing_event = asyncio.Event()
        self._tts_task: asyncio.Task[None] | None = None

    async def is_executing(self) -> bool:
        return self._executing_event.is_set()

    async def set_executing(self, value: bool) -> None:
        async with self._lock:
            if value:
                self._executing_event.set()
                self._interrupt_event.clear()
            else:
                self._executing_event.clear()

    async def request_interrupt(self) -> None:
        async with self._lock:
            self._interrupt_event.set()
            tts_task = self._tts_task

        if tts_task and not tts_task.done():
            tts_task.cancel()

    async def clear_interrupt(self) -> None:
        self._interrupt_event.clear()

    async def should_interrupt(self) -> bool:
        return self._interrupt_event.is_set()

    async def register_tts_task(self, task: asyncio.Task[None]) -> None:
        async with self._lock:
            self._tts_task = task

    async def clear_tts_task(self, task: asyncio.Task[None] | None = None) -> None:
        async with self._lock:
            if task is None or self._tts_task is task:
                self._tts_task = None


class PipelineEmitter(DisplayEmitter):
    """Bridges processor callbacks to display broadcasting and execution control."""

    def __init__(self, broadcaster: DisplayBroadcaster, control: ExecutionControl) -> None:
        self._broadcaster = broadcaster
        self._control = control

    async def emit_display(self, event: dict[str, Any]) -> None:
        await self._broadcaster.broadcast(event)

    async def should_interrupt(self) -> bool:
        return await self._control.should_interrupt()

    async def request_interrupt(self) -> None:
        await self._control.request_interrupt()

    async def clear_interrupt(self) -> None:
        await self._control.clear_interrupt()

    async def set_executing(self, value: bool) -> None:
        await self._control.set_executing(value)

    async def register_tts_task(self, task: asyncio.Task[None]) -> None:
        await self._control.register_tts_task(task)

    async def clear_tts_task(self, task: asyncio.Task[None] | None = None) -> None:
        await self._control.clear_tts_task(task)


class PipecatClawVoiceService:
    def __init__(self, broadcaster: DisplayBroadcaster, *, success_rate: float = 0.68) -> None:
        self._queue_lock = asyncio.Lock()
        self._control = ExecutionControl()
        self._emitter = PipelineEmitter(broadcaster, self._control)
        self._claw_controller = ClawController(
            ClawControllerConfig.from_env(success_rate=success_rate)
        )
        self._runner = PipelineRunner()
        self._runner_task: asyncio.Task[None] | None = None

        pipeline = Pipeline(
            [
                VoiceStartProcessor(self._emitter),
                SpeechToTextProcessor(self._emitter),
                AgentProcessor(self._emitter, self._claw_controller),
                CartesiaMarkupProcessor(self._emitter),
                TTSSpeakProcessor(self._emitter),
                DisplayEventDispatchProcessor(self._emitter),
            ]
        )
        self._task = PipelineTask(pipeline)

    async def _queue_frame(self, frame: Any) -> None:
        async with self._queue_lock:
            await self._task.queue_frame(frame)

    async def _submit_text_frame(
        self,
        text: str,
        frame_type: type[UtteranceFrame] | type[RawTextFrame],
        *,
        source: str,
    ) -> None:
        normalized = text.strip()
        if not normalized:
            return
        if await self._control.is_executing():
            await self._control.request_interrupt()
        await self._queue_frame(frame_type(text=normalized, source=source))

    async def start(self) -> None:
        if self._runner_task is not None and self._runner_task.done():
            self._runner_task = None
        if self._runner_task is not None:
            return
        await self._claw_controller.start()
        self._runner_task = asyncio.create_task(self._runner.run(self._task))

    async def stop(self) -> None:
        await self._task.cancel()
        if self._runner_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None
        await self._claw_controller.stop()

    async def submit_utterance(self, text: str, *, source: str = "stdin") -> None:
        await self._submit_text_frame(text, UtteranceFrame, source=source)

    async def submit_raw_text(self, text: str, *, source: str = "text") -> None:
        await self._submit_text_frame(text, RawTextFrame, source=source)

    async def submit_display_event(self, event: dict[str, Any]) -> None:
        await self._queue_frame(DisplayEventFrame(event=event))

    async def submit_display_events(self, events: Iterable[dict[str, Any]]) -> None:
        async with self._queue_lock:
            for event in events:
                await self._task.queue_frame(DisplayEventFrame(event=event))

    async def on_speech_started(self, source: str = "mic") -> None:
        if not await self._control.is_executing():
            return

        await self._control.request_interrupt()
        await self.submit_display_events(
            [
                {"type": "effect", "effect": "voiceDetected"},
                {"type": "state", "state": "listening"},
            ]
        )
