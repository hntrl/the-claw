from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, Awaitable, Callable, Literal

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    LLMSetToolsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioNoiseReduction,
    InputAudioTranscription,
    SemanticTurnDetection,
    SessionProperties,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from common.audio_output import LocalAudioOutput
from common.broadcaster import DisplayBroadcaster
from common.claw_controller import ClawController, ClawControllerConfig

logger = logging.getLogger(__name__)

MotionDirection = Literal["left", "right", "forward", "back", "down", "up"]
InputGateDecision = Literal[
    "forward",
    "forward_barge",
    "drop_ducking",
    "drop_rms",
    "drop_barge_duration",
]
AgentStep = Literal[
    "speech_to_text",
    "intent_parse",
    "target_select",
    "motion_plan",
    "claw_execute",
    "result_evaluate",
]

AGENT_STEPS: tuple[AgentStep, ...] = (
    "speech_to_text",
    "intent_parse",
    "target_select",
    "motion_plan",
    "claw_execute",
    "result_evaluate",
)

EMOTION_MAP: dict[str, str] = {
    "neutral": "calm",
    "calm": "calm",
    "peaceful": "calm",
    "content": "calm",
    "excited": "excited",
    "happy": "excited",
    "enthusiastic": "excited",
    "surprised": "surprised",
    "amazed": "surprised",
    "confused": "confused",
    "curious": "suspicious",
    "skeptical": "suspicious",
    "thinking": "thinking",
    "nervous": "nervous",
    "anxious": "nervous",
    "scared": "nervous",
    "stressed": "stressed",
    "angry": "stressed",
    "frustrated": "stressed",
    "sad": "disappointed",
    "disappointed": "disappointed",
    "affectionate": "love",
    "love": "love",
}

SYSTEM_PROMPT_TEMPLATE = """You are ClawPilot, a voice agent for a DIY claw machine.

Personality: playfully sassy, theatrical, brief, but not overwhelmingly slow (1-2 sentences).

Machine basics:
- Three axes: X (forward/back), Y (left/right), Z (up/down cable).
- Motion uses DEGREES of motor rotation.
  X: {x_degrees_per_foot:.0f} deg ~= 1 foot
  Y: {y_degrees_per_foot:.0f} deg ~= 1 foot
  Z: {z_degrees_per_foot:.0f} deg ~= 1 foot
- Sign conventions:
  X positive=forward, negative=backward
  Y positive=right, negative=left
  Z positive=down, negative=up

Tools:
- move_axis(axis, degrees)
- lower_claw(degrees?) default {default_lower_degrees:.0f}
- raise_claw()
- open_claw(angle?)
- close_claw(angle?)
- home_z()
- home()
- get_state()
- halt()
- reset_emergency()
- set_expression(mood, emotion?) for frontend mood only (optional).

Rules:
- Prefer small, incremental moves unless user asks for a large move.
- Call halt() first for urgent stop/wait requests.
- If a tool returns status LIMIT, mention it briefly.
- If a tool returns status STUCK, say a motor appears stuck and stop chaining moves.
- If state shows z_homed is false and user asks for lower/grab/deliver behavior, suggest home_z() first.
- Motion/action tools may be accepted asynchronously; do not invent completion details.
- If confirmation is needed after motion, call get_state().
- Never mention internal runtime mechanics like queues, queue depth, pending jobs, function calls, tools, or status polling.
- Speak only user-facing claw actions/results (for example: moving, grabbing, stopping), never implementation details.
- For non-action questions, answer briefly without motion tools.
- End each turn with one short spoken sentence.
"""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    with contextlib.suppress(ValueError):
        return float(value.strip())
    return default


class RealtimeFrameObserverProcessor(FrameProcessor):
    """Observes realtime frames and mirrors key state to frontend events."""

    def __init__(self, service: "RealtimeClawVoiceService") -> None:
        super().__init__()
        self._service = service

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            await self._service._on_interim_transcript(frame.text)
        elif isinstance(frame, TranscriptionFrame):
            await self._service._on_final_transcript(frame.text)
        elif isinstance(frame, UserStartedSpeakingFrame):
            await self._service._on_user_started_speaking_event()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self._service._on_user_stopped_speaking_event()
        elif isinstance(frame, ErrorFrame):
            await self._service._on_error(frame)

        await self.push_frame(frame, direction)


class RealtimeAudioOutputProcessor(FrameProcessor):
    """Shared local speaker output sink for realtime audio frames."""

    def __init__(self, service: "RealtimeClawVoiceService") -> None:
        super().__init__()
        self._service = service
        self._bot_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSAudioRawFrame):
            if not self._bot_speaking:
                self._bot_speaking = True
                await self.push_frame(BotStartedSpeakingFrame(), direction)
            await self._service._write_audio_bytes(
                frame.audio, sample_rate=frame.sample_rate
            )
            return
        if isinstance(frame, TTSStoppedFrame):
            await self._service._flush_audio_buffer()
            if self._bot_speaking:
                self._bot_speaking = False
                await self.push_frame(BotStoppedSpeakingFrame(), direction)
            return

        await self.push_frame(frame, direction)


class RealtimeClawVoiceService:
    """Realtime runtime built around Pipecat's context-aggregator pattern."""

    def __init__(
        self, broadcaster: DisplayBroadcaster, *, success_rate: float = 0.68
    ) -> None:
        self._broadcaster = broadcaster
        self._claw_controller = ClawController(
            ClawControllerConfig.from_env(success_rate=success_rate)
        )

        self._api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is required for realtime runtime")

        self._model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
        self._voice = os.getenv("OPENAI_REALTIME_VOICE", "marin")
        self._turn_eagerness: Literal["low", "medium", "high", "auto"] | None = None
        turn_eagerness_raw = (
            os.getenv("OPENAI_REALTIME_TURN_EAGERNESS", "low").strip().lower()
        )
        if turn_eagerness_raw in {"low", "medium", "high", "auto"}:
            self._turn_eagerness = turn_eagerness_raw
        self._turn_create_response = _env_bool(
            "OPENAI_REALTIME_TURN_CREATE_RESPONSE", True
        )
        self._turn_interrupt_response = _env_bool(
            "OPENAI_REALTIME_TURN_INTERRUPT_RESPONSE", False
        )
        self._barge_in_enabled = _env_bool("OPENAI_REALTIME_BARGE_IN_ENABLED", False)
        barge_in_grace_raw = os.getenv("OPENAI_REALTIME_BARGE_IN_GRACE_MS", "900")
        try:
            self._barge_in_grace_s = max(0.0, int(barge_in_grace_raw) / 1000)
        except ValueError:
            self._barge_in_grace_s = 0.9
        self._input_ducking_enabled = _env_bool("OPENAI_REALTIME_INPUT_DUCKING", True)
        ducking_tail_raw = os.getenv("OPENAI_REALTIME_DUCKING_TAIL_MS", "280")
        try:
            self._input_ducking_tail_s = max(0.0, int(ducking_tail_raw) / 1000)
        except ValueError:
            self._input_ducking_tail_s = 0.28
        self._barge_in_min_rms = _env_float("OPENAI_REALTIME_BARGE_IN_MIN_RMS", 0.030)
        barge_in_min_raw = os.getenv("OPENAI_REALTIME_BARGE_IN_MIN_MS", "120")
        try:
            self._barge_in_min_s = max(0.0, int(barge_in_min_raw) / 1000)
        except ValueError:
            self._barge_in_min_s = 0.12
        self._input_gate_log_enabled = _env_bool(
            "OPENAI_REALTIME_INPUT_GATE_LOG", False
        )
        self._input_gate_log_interval_s = max(
            1.0,
            _env_float("OPENAI_REALTIME_INPUT_GATE_LOG_INTERVAL_S", 5.0),
        )
        self._noise_reduction_type: Literal["near_field", "far_field"] = "near_field"
        noise_reduction_raw = (
            os.getenv("OPENAI_REALTIME_NOISE_REDUCTION", "near_field").strip().lower()
        )
        if noise_reduction_raw in {"near_field", "far_field"}:
            self._noise_reduction_type = noise_reduction_raw
        speed_raw = os.getenv("OPENAI_REALTIME_SPEED", "").strip()
        self._voice_speed: float | None = None
        if speed_raw:
            with contextlib.suppress(ValueError):
                parsed = float(speed_raw)
                # Keep speed in a conservative range to avoid provider rejections.
                self._voice_speed = max(0.5, min(2.0, parsed))
        self._input_sample_rate = max(24000, int(os.getenv("MIC_SAMPLE_RATE", "24000")))
        min_input_audio_ms_raw = os.getenv("OPENAI_REALTIME_MIN_INPUT_AUDIO_MS", "120")
        try:
            self._min_input_audio_ms = max(20, int(min_input_audio_ms_raw))
        except ValueError:
            self._min_input_audio_ms = 120
        self._min_input_audio_bytes = max(
            2,
            int(self._input_sample_rate * (self._min_input_audio_ms / 1000.0)) * 2,
        )

        self._play_audio = os.getenv(
            "AGENT_REALTIME_PLAY_AUDIO", "1"
        ).strip().lower() not in {"0", "false", "no"}
        audio_write_timeout_raw = os.getenv(
            "AGENT_REALTIME_AUDIO_WRITE_TIMEOUT_S", "2.0"
        ).strip()
        try:
            self._audio_write_timeout_s = max(0.5, float(audio_write_timeout_raw))
        except ValueError:
            self._audio_write_timeout_s = 2.0

        self._state_lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._input_audio_lock = asyncio.Lock()
        self._text_submit_lock = asyncio.Lock()
        self._tool_call_lock = asyncio.Lock()
        self._queued_tool_calls = 0
        self._inflight_tool_calls = 0
        self._background_tool_tasks: set[asyncio.Task[None]] = set()

        # server.py owns process signals and coordinates websocket/stdin/mic
        # teardown. Letting Pipecat also handle SIGINT can cancel only the
        # pipeline while the outer server keeps waiting on its stop event.
        self._runner = PipelineRunner(handle_sigint=False)
        self._runner_task: asyncio.Task[None] | None = None
        self._task: PipelineTask | None = None
        self._llm: OpenAIRealtimeLLMService | None = None
        self._user_aggregator: Any | None = None
        self._assistant_aggregator: Any | None = None

        self._interrupt_requested = False
        self._executing = False
        self._user_currently_speaking = False
        self._assistant_started_speaking_at = 0.0
        self._assistant_stopped_speaking_at = 0.0
        self._barge_in_voice_accum_s = 0.0
        self._input_gate_counts: dict[str, int] = {
            "forward": 0,
            "forward_barge": 0,
            "drop_ducking": 0,
            "drop_rms": 0,
            "drop_barge_duration": 0,
        }
        self._last_input_gate_log_at = 0.0

        self._audio_output = LocalAudioOutput()
        self._audio_error_logged = False
        self._audio_error_count = 0
        self._pending_input_audio = bytearray()
        self._pending_text_turns: deque[str] = deque()
        self._realtime_recovery_task: asyncio.Task[None] | None = None
        self._last_realtime_recovery_at = 0.0

        self._x_degrees_per_foot = _env_float("CLAW_X_DEGREES_PER_FOOT", 540.0)
        self._y_degrees_per_foot = _env_float("CLAW_Y_DEGREES_PER_FOOT", 540.0)
        self._z_degrees_per_foot = _env_float("CLAW_Z_DEGREES_PER_FOOT", 360.0)
        self._default_lower_degrees = _env_float(
            "CLAW_DEFAULT_LOWER_DEGREES",
            self._z_degrees_per_foot * 3,
        )
        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            x_degrees_per_foot=self._x_degrees_per_foot,
            y_degrees_per_foot=self._y_degrees_per_foot,
            z_degrees_per_foot=self._z_degrees_per_foot,
            default_lower_degrees=self._default_lower_degrees,
        )

        self._tools_schema = self._build_tools_schema()

        self._turn_state: dict[str, Any] = {}
        self._reset_turn_state(user_text="", thinking_started=False, stt_complete=False)

    async def start(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            return

        await self._claw_controller.start()
        self._llm = self._build_llm_service()

        context = LLMContext(
            [
                {
                    "role": "developer",
                    "content": "Wait for user input before speaking. Use tools to execute claw actions.",
                }
            ],
            self._tools_schema,
        )
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(),
        )
        self._user_aggregator = user_aggregator
        self._assistant_aggregator = assistant_aggregator

        @user_aggregator.event_handler("on_user_turn_stopped")
        async def _on_user_turn_stopped(
            _aggregator: Any,
            _strategy: Any,
            message: UserTurnStoppedMessage,
        ) -> None:
            await self._on_user_turn_stopped_message(message)

        @assistant_aggregator.event_handler("on_assistant_turn_started")
        async def _on_assistant_turn_started(*_args: Any) -> None:
            await self._on_assistant_turn_started_message()

        @assistant_aggregator.event_handler("on_assistant_turn_stopped")
        async def _on_assistant_turn_stopped(
            _aggregator: Any,
            message: AssistantTurnStoppedMessage,
        ) -> None:
            await self._on_assistant_turn_stopped_message(message)

        pipeline = Pipeline(
            [
                user_aggregator,
                RealtimeFrameObserverProcessor(self),
                self._llm,
                RealtimeAudioOutputProcessor(self),
                assistant_aggregator,
            ]
        )
        self._task = PipelineTask(pipeline)
        self._runner_task = asyncio.create_task(self._runner.run(self._task))

        await self._wait_until_session_ready()
        await self._queue_frame(LLMSetToolsFrame(tools=self._tools_schema))

    async def stop(self) -> None:
        if self._realtime_recovery_task is not None:
            self._realtime_recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._realtime_recovery_task
            self._realtime_recovery_task = None
        if self._background_tool_tasks:
            for task in tuple(self._background_tool_tasks):
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(
                    *self._background_tool_tasks, return_exceptions=True
                )
            self._background_tool_tasks.clear()
        if self._task is not None:
            await self._task.cancel()
        if self._runner_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._runner_task), timeout=3.0)
            except TimeoutError:
                self._runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(self._runner_task, timeout=2.0)
            self._runner_task = None

        self._task = None
        self._llm = None
        self._user_aggregator = None
        self._assistant_aggregator = None
        async with self._input_audio_lock:
            self._pending_input_audio.clear()
        self._pending_text_turns.clear()
        await self._audio_output.close()
        await self._claw_controller.stop()

    async def submit_utterance(self, text: str, *, source: str = "stdin") -> None:
        await self._submit_text(text, source=source, raw=False)

    async def submit_raw_text(self, text: str, *, source: str = "text") -> None:
        await self._submit_text(text, source=source, raw=True)

    async def submit_audio_input(
        self, audio_bytes: bytes, *, source: str = "mic"
    ) -> None:
        del source
        if not audio_bytes:
            return
        # Realtime uses 16-bit mono PCM. Drop malformed sub-sample fragments
        # instead of letting them become 0ms/invalid input buffer writes.
        if len(audio_bytes) < 2:
            return
        if len(audio_bytes) % 2:
            audio_bytes = audio_bytes[:-1]
            if not audio_bytes:
                return

        decision = self._classify_live_audio_chunk(audio_bytes)
        self._record_input_gate_decision(decision)
        if decision.startswith("drop"):
            async with self._input_audio_lock:
                self._pending_input_audio.clear()
            return
        if decision == "forward_barge" and not await self._should_interrupt():
            await self.request_interrupt()
        await self._ensure_started()
        frame_audio = await self._buffer_live_audio_chunk(audio_bytes)
        if frame_audio is None:
            return
        await self._queue_input_audio_frame(frame_audio)

    async def _buffer_live_audio_chunk(self, audio_bytes: bytes) -> bytes | None:
        async with self._input_audio_lock:
            self._pending_input_audio.extend(audio_bytes)
            if len(self._pending_input_audio) < self._min_input_audio_bytes:
                return None
            frame_audio = bytes(self._pending_input_audio)
            self._pending_input_audio.clear()
            return frame_audio

    async def _queue_input_audio_frame(self, audio_bytes: bytes) -> None:
        if len(audio_bytes) < self._min_input_audio_bytes:
            return
        await self._queue_frame(
            InputAudioRawFrame(
                audio=audio_bytes,
                sample_rate=self._input_sample_rate,
                num_channels=1,
            )
        )

    async def on_speech_started(self, source: str = "mic") -> None:
        del source
        if not await self.is_executing():
            return

        await self.request_interrupt()
        await self._emit({"type": "effect", "effect": "voiceDetected"})
        await self._emit({"type": "state", "state": "listening"})

    async def is_executing(self) -> bool:
        async with self._state_lock:
            return self._executing

    async def request_interrupt(self) -> None:
        async with self._state_lock:
            self._interrupt_requested = True

        await self._queue_frame(InterruptionFrame())
        await self._audio_output.close()
        await self._emit({"type": "emotion_clear"})

    async def _clear_interrupt(self) -> None:
        async with self._state_lock:
            self._interrupt_requested = False

    async def _set_executing(self, value: bool) -> None:
        async with self._state_lock:
            self._executing = value
            if value:
                self._interrupt_requested = False

    async def _should_interrupt(self) -> bool:
        async with self._state_lock:
            return self._interrupt_requested

    async def _ensure_started(self) -> None:
        if self._runner_task is None or self._runner_task.done():
            await self.start()

    async def _queue_frame(self, frame: Frame) -> None:
        if self._task is None:
            return
        async with self._queue_lock:
            await self._task.queue_frame(frame)

    async def _wait_until_session_ready(self, timeout_s: float = 8.0) -> None:
        start = asyncio.get_running_loop().time()
        while True:
            if self._llm is not None and getattr(
                self._llm, "_api_session_ready", False
            ):
                return
            if self._runner_task is not None and self._runner_task.done():
                raise RuntimeError(
                    "Realtime pipeline exited before session became ready"
                )
            if asyncio.get_running_loop().time() - start >= timeout_s:
                logger.warning(
                    "Timed out waiting for realtime session.updated; continuing"
                )
                return
            await asyncio.sleep(0.05)

    async def _emit(self, event: dict[str, Any]) -> None:
        await self._broadcaster.broadcast(event)

    async def _emit_step(self, step: AgentStep, status: str) -> None:
        await self._emit({"type": "agent_step", "step": step, "status": status})

    async def _begin_turn(self, *, from_audio: bool) -> None:
        await self._set_executing(True)
        self._reset_turn_state(user_text="", thinking_started=False, stt_complete=False)
        for step in AGENT_STEPS:
            await self._emit_step(step, "pending")

        if from_audio:
            await self._emit({"type": "effect", "effect": "voiceDetected"})
            await self._emit({"type": "state", "state": "listening"})
            await self._emit_step("speech_to_text", "active")

    async def _submit_text(self, text: str, *, source: str, raw: bool) -> None:
        del source
        del raw
        normalized = text.strip()
        if not normalized:
            return

        await self._ensure_started()
        async with self._text_submit_lock:
            has_inflight_tools = await self._has_inflight_tool_calls()
            is_executing = await self.is_executing()

            if is_executing and has_inflight_tools:
                self._pending_text_turns.append(normalized)
                await self.request_interrupt()
                return

            if is_executing:
                await self.request_interrupt()
                await self._set_executing(False)

            await self._start_text_turn(normalized)

    async def _start_text_turn(self, normalized: str) -> None:
        # Normalize speech-state flags for text turns.
        self._assistant_started_speaking_at = 0.0
        self._assistant_stopped_speaking_at = time.monotonic()
        self._barge_in_voice_accum_s = 0.0
        self._user_currently_speaking = False

        await self._begin_turn(from_audio=False)
        await self._emit_step("speech_to_text", "active")
        await self._emit({"type": "transcript", "text": normalized, "isFinal": True})
        await self._emit_step("speech_to_text", "complete")

        self._reset_turn_state(
            user_text=normalized, thinking_started=True, stt_complete=True
        )
        await self._emit({"type": "state", "state": "thinking"})
        await self._emit_step("intent_parse", "active")

        prompt_text = await self._build_user_turn_input(normalized)
        await self._queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": prompt_text}],
                run_llm=True,
            )
        )

    async def _start_next_pending_text_turn(self) -> None:
        async with self._text_submit_lock:
            if await self.is_executing():
                return
            if not self._pending_text_turns:
                return
            next_text = self._pending_text_turns.popleft()
            await self._start_text_turn(next_text)

    def _reset_turn_state(
        self, *, user_text: str, thinking_started: bool, stt_complete: bool
    ) -> None:
        self._turn_state = {
            "user_text": user_text,
            "intent_complete": False,
            "ran_execute": False,
            "thinking_started": thinking_started,
            "stt_complete": stt_complete,
        }

    def _ensure_turn_state(self) -> dict[str, Any]:
        if not self._turn_state:
            self._reset_turn_state(
                user_text="", thinking_started=False, stt_complete=False
            )
        return self._turn_state

    async def _on_interim_transcript(self, text: str) -> None:
        content = text.strip()
        if not content:
            return
        await self._emit_step("speech_to_text", "active")
        await self._emit({"type": "transcript", "text": content, "isFinal": False})

    async def _on_final_transcript(self, text: str) -> None:
        content = text.strip()
        if not content:
            return

        turn_state = self._ensure_turn_state()
        turn_state["user_text"] = content

        await self._emit({"type": "transcript", "text": content, "isFinal": True})
        if not turn_state["stt_complete"]:
            await self._emit_step("speech_to_text", "complete")
            turn_state["stt_complete"] = True
        if not turn_state["thinking_started"]:
            await self._emit({"type": "state", "state": "thinking"})
            await self._emit_step("intent_parse", "active")
            turn_state["thinking_started"] = True

    async def _on_user_started_speaking_event(self) -> None:
        if await self.is_executing():
            if not self._barge_in_enabled:
                return
            if (
                self._assistant_started_speaking_at > 0
                and (time.monotonic() - self._assistant_started_speaking_at)
                < self._barge_in_grace_s
            ):
                return
            if self._user_currently_speaking:
                return
            self._user_currently_speaking = True
            await self.request_interrupt()
            await self._emit({"type": "effect", "effect": "voiceDetected"})
            await self._emit({"type": "state", "state": "listening"})
            await self._emit_step("speech_to_text", "active")
            return

        if self._user_currently_speaking:
            return
        self._user_currently_speaking = True
        await self._begin_turn(from_audio=True)

    async def _on_user_stopped_speaking_event(self) -> None:
        self._user_currently_speaking = False
        await self._emit({"type": "state", "state": "transcribing"})

    async def _on_user_turn_stopped_message(
        self, message: UserTurnStoppedMessage
    ) -> None:
        content = (message.content or "").strip()
        if not content:
            return

        turn_state = self._ensure_turn_state()
        turn_state["user_text"] = content

        await self._emit({"type": "transcript", "text": content, "isFinal": True})
        if not turn_state["stt_complete"]:
            await self._emit_step("speech_to_text", "complete")
            turn_state["stt_complete"] = True
        if not turn_state["thinking_started"]:
            await self._emit({"type": "state", "state": "thinking"})
            await self._emit_step("intent_parse", "active")
            turn_state["thinking_started"] = True

    async def _on_assistant_turn_started_message(self) -> None:
        await self._set_executing(True)
        self._assistant_started_speaking_at = time.monotonic()
        self._assistant_stopped_speaking_at = 0.0
        self._barge_in_voice_accum_s = 0.0
        turn_state = self._ensure_turn_state()
        if not turn_state["thinking_started"]:
            await self._emit({"type": "state", "state": "thinking"})
            await self._emit_step("intent_parse", "active")
            turn_state["thinking_started"] = True

    async def _on_assistant_turn_stopped_message(
        self, message: AssistantTurnStoppedMessage
    ) -> None:
        self._assistant_started_speaking_at = 0.0
        self._assistant_stopped_speaking_at = time.monotonic()
        self._barge_in_voice_accum_s = 0.0
        self._user_currently_speaking = False
        turn_state = self._ensure_turn_state()

        if not turn_state["stt_complete"]:
            await self._emit_step("speech_to_text", "complete")
            turn_state["stt_complete"] = True
        if turn_state["thinking_started"] and not turn_state["intent_complete"]:
            await self._mark_intent_complete(turn_state, emit_effect=False)

        has_inflight_tools = await self._has_inflight_tool_calls()
        await self._emit({"type": "emotion_clear"})
        if not message.interrupted and not has_inflight_tools:
            await self._emit({"type": "state", "state": "attract"})

        await self._clear_interrupt()
        if not has_inflight_tools:
            await self._set_executing(False)
            await self._start_next_pending_text_turn()

    async def _on_error(self, frame: ErrorFrame) -> None:
        message = str(frame.error)
        if self._is_recoverable_realtime_session_error(message):
            logger.debug(
                "Recovering realtime session after recoverable error: %s", message
            )
            await self._schedule_realtime_recovery(message)
            return

        logger.warning("Realtime pipeline error: %s", frame.error)
        await self._emit({"type": "state", "state": "error"})
        for step in AGENT_STEPS:
            await self._emit_step(step, "error")
        await self._emit({"type": "effect", "effect": "error"})
        await self._emit({"type": "emotion_clear"})
        await self._emit({"type": "state", "state": "attract"})
        await self._clear_interrupt()
        await self._set_executing(False)
        await self._start_next_pending_text_turn()

    def _is_recoverable_realtime_session_error(self, message: str) -> bool:
        normalized = message.lower()
        return (
            "no active response found" in normalized
            or "active response in progress" in normalized
            or "conversation already has an active response" in normalized
            or "response failed" in normalized
            or "response expired" in normalized
            or "response not found" in normalized
            or "already shorter than" in normalized
            or "input_audio_buffer_commit_empty" in normalized
            or "buffer too small" in normalized
            or "audio buffer" in normalized
            or "keepalive ping timeout" in normalized
            or "connection closed" in normalized
            or "connection reset by peer" in normalized
        )

    async def _schedule_realtime_recovery(self, reason: str) -> None:
        if (
            self._realtime_recovery_task is not None
            and not self._realtime_recovery_task.done()
        ):
            return
        self._realtime_recovery_task = asyncio.create_task(
            self._recover_realtime_session(reason)
        )

    async def _recover_realtime_session(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_realtime_recovery_at < 1.0:
            return
        self._last_realtime_recovery_at = now

        async with self._input_audio_lock:
            self._pending_input_audio.clear()

        llm = self._llm
        if llm is None:
            return

        logger.warning("Resetting realtime session after recoverable error: %s", reason)
        self._assistant_started_speaking_at = 0.0
        self._assistant_stopped_speaking_at = 0.0
        self._barge_in_voice_accum_s = 0.0
        self._user_currently_speaking = False
        await self._clear_interrupt()
        await self._emit({"type": "state", "state": "listening"})
        try:
            await llm.reset_conversation()
            await self._queue_frame(LLMSetToolsFrame(tools=self._tools_schema))
            has_inflight_tools = await self._has_inflight_tool_calls()
            if not has_inflight_tools:
                await self._set_executing(False)
                await self._emit({"type": "state", "state": "attract"})
                await self._start_next_pending_text_turn()
        except Exception as exc:
            logger.warning("Realtime session reset failed: %s", exc)
            await self._emit({"type": "state", "state": "error"})

    def _build_tools_schema(self) -> ToolsSchema:
        move_axis = FunctionSchema(
            name="move_axis",
            description=(
                "Move one axis by signed degrees. "
                "axis: x/y/z. X positive=forward, Y positive=right, Z positive=down."
            ),
            properties={
                "axis": {"type": "string", "enum": ["x", "y", "z"]},
                "degrees": {"type": "number"},
            },
            required=["axis", "degrees"],
        )
        open_claw = FunctionSchema(
            name="open_claw",
            description="Open claw servo. Optional angle 25-90.",
            properties={},
            required=[],
        )
        lower_claw = FunctionSchema(
            name="lower_claw",
            description=(
                "Lower claw by positive degrees. If omitted, uses default depth."
            ),
            properties={"degrees": {"type": "number", "minimum": 0}},
            required=[],
        )
        raise_claw = FunctionSchema(
            name="raise_claw",
            description="Retract claw to top limit (Z=0 reference).",
            properties={},
            required=[],
        )
        close_claw = FunctionSchema(
            name="close_claw",
            description="Close claw servo. Optional angle 25-90.",
            properties={},
            required=[],
        )
        home_z = FunctionSchema(
            name="home_z",
            description="Home Z only by driving upward to top limit switch.",
            properties={},
            required=[],
        )
        home = FunctionSchema(
            name="home",
            description="Run full homing and park/dropoff sequence.",
            properties={},
            required=[],
        )
        get_state = FunctionSchema(
            name="get_state",
            description="Get current machine state snapshot.",
            properties={},
            required=[],
        )
        halt = FunctionSchema(
            name="halt",
            description="Emergency stop. Halts motion immediately.",
            properties={},
            required=[],
        )
        reset_emergency = FunctionSchema(
            name="reset_emergency",
            description="Clear emergency-stop latch.",
            properties={},
            required=[],
        )
        set_expression = FunctionSchema(
            name="set_expression",
            description="Set frontend expression mood for the current turn.",
            properties={
                "mood": {
                    "type": "string",
                    "enum": [
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
                },
                "emotion": {"type": "string"},
            },
            required=["mood"],
        )
        return ToolsSchema(
            standard_tools=[
                move_axis,
                open_claw,
                lower_claw,
                raise_claw,
                close_claw,
                home_z,
                home,
                get_state,
                halt,
                reset_emergency,
                set_expression,
            ]
        )

    def _build_llm_service(self) -> OpenAIRealtimeLLMService:
        settings = OpenAIRealtimeLLMService.Settings(
            system_instruction=self._system_prompt,
            session_properties=SessionProperties(
                output_modalities=["audio"],
                audio=AudioConfiguration(
                    input=AudioInput(
                        transcription=InputAudioTranscription(),
                        turn_detection=SemanticTurnDetection(
                            eagerness=self._turn_eagerness,
                            create_response=self._turn_create_response,
                            interrupt_response=(
                                self._turn_interrupt_response
                                if self._barge_in_enabled
                                else False
                            ),
                        ),
                        noise_reduction=InputAudioNoiseReduction(
                            type=self._noise_reduction_type
                        ),
                    ),
                    output=AudioOutput(voice=self._voice, speed=self._voice_speed),
                ),
                tools=self._tools_schema,
                tool_choice="auto",
            ),
        )

        llm = OpenAIRealtimeLLMService(
            api_key=self._api_key,
            model=self._model,
            settings=settings,
        )
        llm.register_function("move_axis", self._handle_move_axis)
        llm.register_function("open_claw", self._handle_open_claw)
        llm.register_function("lower_claw", self._handle_lower_claw)
        llm.register_function("raise_claw", self._handle_raise_claw)
        llm.register_function("close_claw", self._handle_close_claw)
        llm.register_function("home_z", self._handle_home_z)
        llm.register_function("home", self._handle_home)
        llm.register_function("get_state", self._handle_get_state)
        llm.register_function("halt", self._handle_halt)
        llm.register_function("reset_emergency", self._handle_reset_emergency)
        llm.register_function("set_expression", self._handle_set_expression)
        return llm

    async def _write_audio_bytes(self, pcm: bytes, *, sample_rate: int) -> None:
        if not self._play_audio or await self._should_interrupt() or not pcm:
            return
        if len(pcm) % 2 != 0:
            pcm = pcm[:-1]
        if not pcm:
            return

        try:
            await asyncio.wait_for(
                self._audio_output.write(
                    pcm,
                    sample_rate=sample_rate,
                    dtype="int16",
                    channels=1,
                ),
                timeout=self._audio_write_timeout_s,
            )
            self._audio_error_count = 0
        except TimeoutError:
            self._audio_error_count += 1
            logger.warning(
                "Realtime audio playback timed out after %.1fs (attempt %s); retrying",
                self._audio_write_timeout_s,
                self._audio_error_count,
            )
            await self._audio_output.close()
            if self._audio_error_count >= 3:
                logger.warning(
                    "Realtime audio playback timed out repeatedly; disabling speaker output"
                )
                self._play_audio = False
        except Exception as exc:
            self._audio_error_count += 1
            if not self._audio_error_logged:
                self._audio_error_logged = True
                logger.warning("Realtime audio playback failed (will retry): %s", exc)
            if self._audio_error_count >= 3:
                logger.warning(
                    "Realtime audio playback failed repeatedly; disabling speaker output"
                )
                self._play_audio = False
            await self._audio_output.close()

    async def _flush_audio_buffer(self) -> None:
        return

    async def _handle_move_axis(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="move_axis",
            run_tool=lambda args: self._tool_move_axis(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_open_claw(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="open_claw",
            run_tool=lambda args: self._tool_open_claw(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_lower_claw(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="lower_claw",
            run_tool=lambda args: self._tool_lower_claw(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_raise_claw(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="raise_claw",
            run_tool=lambda args: self._tool_raise_claw(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_close_claw(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="close_claw",
            run_tool=lambda args: self._tool_close_claw(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_home_z(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="home_z",
            run_tool=lambda args: self._tool_home_z(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_home(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="home",
            run_tool=lambda args: self._tool_home(
                args, turn_state=self._ensure_turn_state()
            ),
            wait_for_result=False,
        )

    async def _handle_get_state(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="get_state",
            run_tool=lambda args: self._tool_get_state(
                args, turn_state=self._ensure_turn_state()
            ),
        )

    async def _handle_halt(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="halt",
            run_tool=lambda args: self._tool_halt(
                args, turn_state=self._ensure_turn_state()
            ),
        )

    async def _handle_reset_emergency(self, params: FunctionCallParams) -> None:
        await self._run_serialized_tool_call(
            params,
            tool_name="reset_emergency",
            run_tool=lambda args: self._tool_reset_emergency(
                args, turn_state=self._ensure_turn_state()
            ),
        )

    async def _handle_set_expression(self, params: FunctionCallParams) -> None:
        # Expression changes are display-only and should remain low-latency.
        args = dict(params.arguments) if isinstance(params.arguments, Mapping) else {}
        result = await self._tool_set_expression(args)
        await params.result_callback(result)

    async def _run_serialized_tool_call(
        self,
        params: FunctionCallParams,
        *,
        tool_name: str,
        run_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        wait_for_result: bool = True,
    ) -> None:
        args = dict(params.arguments) if isinstance(params.arguments, Mapping) else {}
        was_queued = self._tool_call_lock.locked()
        if was_queued:
            self._queued_tool_calls += 1
            logger.debug(
                "Tool call queued: %s (queue_depth=%s)",
                tool_name,
                self._queued_tool_calls,
            )
        if not wait_for_result:
            await self._set_executing(True)
            await self._increment_inflight_tool_calls()
            task = asyncio.create_task(
                self._run_serialized_tool_call_background(
                    tool_name=tool_name,
                    run_tool=run_tool,
                    args=args,
                    was_queued=was_queued,
                )
            )
            self._background_tool_tasks.add(task)
            task.add_done_callback(self._background_tool_tasks.discard)
            await params.result_callback(
                {"ok": True, "accepted": True, "tool": tool_name}
            )
            return
        try:
            async with self._tool_call_lock:
                result = await run_tool(args)
                await params.result_callback(result)
        except Exception as exc:
            logger.exception("Tool call failed: %s", tool_name)
            await params.result_callback(
                {"ok": False, "error": str(exc), "tool": tool_name}
            )
        finally:
            if was_queued:
                self._queued_tool_calls = max(0, self._queued_tool_calls - 1)

    async def _run_serialized_tool_call_background(
        self,
        *,
        tool_name: str,
        run_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        args: dict[str, Any],
        was_queued: bool,
    ) -> None:
        try:
            async with self._tool_call_lock:
                await run_tool(args)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background tool call failed: %s", tool_name)
            await self._emit_step("claw_execute", "error")
            await self._emit_step("result_evaluate", "error")
        finally:
            if was_queued:
                self._queued_tool_calls = max(0, self._queued_tool_calls - 1)
            remaining = await self._decrement_inflight_tool_calls()
            if remaining == 0:
                await self._maybe_finalize_after_tool_calls()

    async def _increment_inflight_tool_calls(self) -> None:
        async with self._state_lock:
            self._inflight_tool_calls += 1

    async def _decrement_inflight_tool_calls(self) -> int:
        async with self._state_lock:
            self._inflight_tool_calls = max(0, self._inflight_tool_calls - 1)
            return self._inflight_tool_calls

    async def _has_inflight_tool_calls(self) -> bool:
        async with self._state_lock:
            return self._inflight_tool_calls > 0

    async def _maybe_finalize_after_tool_calls(self) -> None:
        if self._pending_text_turns:
            self._assistant_started_speaking_at = 0.0
            self._user_currently_speaking = False
            await self._clear_interrupt()
            await self._set_executing(False)
            await self._start_next_pending_text_turn()
            return
        if self._assistant_started_speaking_at > 0:
            return
        if self._user_currently_speaking:
            return
        await self._emit({"type": "emotion_clear"})
        await self._emit({"type": "state", "state": "attract"})
        await self._clear_interrupt()
        await self._set_executing(False)
        await self._start_next_pending_text_turn()

    async def _tool_move_axis(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        await self._begin_command_tool(turn_state)
        axis = str(arguments.get("axis") or "").strip().lower()
        degrees = self._coerce_float(arguments.get("degrees"), 0.0)
        await self._emit_direction_motion(axis=axis, degrees=degrees)
        result = await self._claw_controller.move_axis(axis=axis, degrees=degrees)
        await self._finish_command_tool(result)
        return result

    async def _tool_open_claw(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        await self._begin_command_tool(turn_state)
        result = await self._claw_controller.open_claw(angle=90)
        await self._finish_command_tool(result)
        return result

    async def _tool_lower_claw(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        await self._begin_command_tool(turn_state)
        degrees = self._coerce_float(
            arguments.get("degrees"), self._default_lower_degrees
        )
        await self._emit({"type": "state", "state": "dropping"})
        await self._emit({"type": "effect", "effect": "dropStarted"})
        await self._emit({"type": "claw_motion", "direction": "down", "speed": 0.55})
        result = await self._claw_controller.lower_claw(degrees=abs(degrees))
        await self._finish_command_tool(result)
        return result

    async def _tool_raise_claw(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        await self._begin_command_tool(turn_state)
        await self._emit({"type": "state", "state": "moving"})
        await self._emit({"type": "claw_motion", "direction": "up", "speed": 0.7})
        result = await self._claw_controller.raise_claw()
        await self._finish_command_tool(result)
        return result

    async def _tool_close_claw(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        await self._begin_command_tool(turn_state)
        result = await self._claw_controller.close_claw(angle=25)
        await self._finish_command_tool(result)
        return result

    async def _tool_home_z(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        await self._begin_command_tool(turn_state)
        await self._emit({"type": "state", "state": "moving"})
        result = await self._claw_controller.home_z()
        await self._finish_command_tool(result)
        return result

    async def _tool_home(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        await self._begin_command_tool(turn_state)
        await self._emit({"type": "state", "state": "moving"})
        result = await self._claw_controller.home()
        await self._finish_command_tool(result)
        return result

    async def _tool_get_state(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        await self._mark_intent_complete(turn_state)
        state = await self._claw_controller.get_state()
        return {"ok": True, "state": state}

    async def _tool_halt(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        await self._begin_command_tool(turn_state)
        result = await self._claw_controller.halt()
        await self._finish_command_tool(result)
        return result

    async def _tool_reset_emergency(
        self, arguments: dict[str, Any], *, turn_state: dict[str, Any]
    ) -> dict[str, Any]:
        del arguments
        await self._begin_command_tool(turn_state)
        result = await self._claw_controller.reset_emergency()
        await self._finish_command_tool(result)
        return result

    async def _tool_set_expression(self, arguments: dict[str, Any]) -> dict[str, Any]:
        mood = str(arguments.get("mood") or "").strip()
        if not mood:
            return {"ok": False, "error": "missing_mood"}
        emotion = str(arguments.get("emotion") or mood).strip().lower()
        normalized_mood = EMOTION_MAP.get(mood.lower(), mood)
        await self._emit(
            {"type": "emotion", "mood": normalized_mood, "emotion": emotion}
        )
        return {"ok": True, "mood": normalized_mood, "emotion": emotion}

    async def _mark_intent_complete(
        self, turn_state: dict[str, Any], *, emit_effect: bool = True
    ) -> None:
        if turn_state["intent_complete"]:
            return
        turn_state["intent_complete"] = True
        await self._emit_step("intent_parse", "complete")
        await self._emit_step("target_select", "complete")
        await self._emit_step("motion_plan", "complete")
        if emit_effect:
            await self._emit({"type": "effect", "effect": "commandParsed"})

    async def _begin_command_tool(self, turn_state: dict[str, Any]) -> None:
        await self._mark_intent_complete(turn_state)
        turn_state["ran_execute"] = True
        await self._emit_step("claw_execute", "active")

    async def _finish_command_tool(self, result: dict[str, Any]) -> None:
        if bool(result.get("ok")):
            await self._emit_step("claw_execute", "complete")
            await self._emit_step("result_evaluate", "active")
            await self._emit_step("result_evaluate", "complete")
            return
        await self._emit_step("claw_execute", "error")
        await self._emit_step("result_evaluate", "error")

    async def _emit_direction_motion(self, *, axis: str, degrees: float) -> None:
        direction: MotionDirection | None = None
        if axis == "x":
            direction = "forward" if degrees > 0 else "back"
        elif axis == "y":
            direction = "right" if degrees > 0 else "left"
        elif axis == "z":
            direction = "down" if degrees > 0 else "up"

        if direction is None or degrees == 0:
            return

        if direction == "down":
            await self._emit({"type": "state", "state": "dropping"})
            await self._emit({"type": "effect", "effect": "dropStarted"})
            speed = 0.55
        else:
            await self._emit({"type": "state", "state": "moving"})
            speed = 0.8
        await self._emit(
            {"type": "claw_motion", "direction": direction, "speed": speed}
        )

    async def _build_user_turn_input(self, user_text: str) -> str:
        try:
            state = await self._claw_controller.get_state()
        except Exception:
            return user_text
        fsm = str(state.get("fsm_state") or "UNKNOWN")
        z_value = state.get("z")
        z_homed = bool(state.get("z_homed"))
        z_homed_str = "YES" if z_homed else "NO (Z position is a guess until homed)"
        status_banner = (
            f"[machine status: fsm={fsm}, Z={z_value}, Z_homed={z_homed_str}]"
        )
        return f"{status_banner}\n{user_text}"

    def _coerce_float(self, value: Any, default: float) -> float:
        with contextlib.suppress(TypeError, ValueError):
            return float(value)
        return default

    def _coerce_int_or_none(self, value: Any) -> int | None:
        if value is None:
            return None
        with contextlib.suppress(TypeError, ValueError):
            return int(value)
        return None

    def _classify_live_audio_chunk(self, audio_bytes: bytes) -> InputGateDecision:
        if not self._input_ducking_enabled:
            return "forward"

        now = time.monotonic()
        assistant_speaking = self._assistant_started_speaking_at > 0
        in_tail = (
            not assistant_speaking
            and self._assistant_stopped_speaking_at > 0
            and (now - self._assistant_stopped_speaking_at) < self._input_ducking_tail_s
        )
        if not assistant_speaking and not in_tail:
            self._barge_in_voice_accum_s = 0.0
            return "forward"

        if not self._barge_in_enabled:
            self._barge_in_voice_accum_s = 0.0
            return "drop_ducking"

        rms = self._pcm16_rms(audio_bytes)
        if rms < self._barge_in_min_rms:
            self._barge_in_voice_accum_s = 0.0
            return "drop_rms"

        chunk_seconds = len(audio_bytes) / (2 * self._input_sample_rate)
        self._barge_in_voice_accum_s += max(0.0, chunk_seconds)
        if self._barge_in_voice_accum_s < self._barge_in_min_s:
            return "drop_barge_duration"
        return "forward_barge"

    def _pcm16_rms(self, audio_bytes: bytes) -> float:
        if len(audio_bytes) < 2:
            return 0.0
        samples = memoryview(audio_bytes).cast("h")
        count = len(samples)
        if count == 0:
            return 0.0
        total = 0.0
        for sample in samples:
            total += float(sample) * float(sample)
        return math.sqrt(total / count) / 32768.0

    def _record_input_gate_decision(self, decision: InputGateDecision) -> None:
        self._input_gate_counts[decision] = self._input_gate_counts.get(decision, 0) + 1
        if not self._input_gate_log_enabled:
            return

        now = time.monotonic()
        if (
            self._last_input_gate_log_at > 0
            and (now - self._last_input_gate_log_at) < self._input_gate_log_interval_s
        ):
            return
        self._last_input_gate_log_at = now
        logger.info(
            "realtime input gate: forward=%s forward_barge=%s drop_ducking=%s drop_rms=%s drop_barge_duration=%s",
            self._input_gate_counts.get("forward", 0),
            self._input_gate_counts.get("forward_barge", 0),
            self._input_gate_counts.get("drop_ducking", 0),
            self._input_gate_counts.get("drop_rms", 0),
            self._input_gate_counts.get("drop_barge_duration", 0),
        )
