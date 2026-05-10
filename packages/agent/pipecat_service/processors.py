from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from common.audio_output import LocalAudioOutput
from common.claw_controller import ClawController
from .frames import (
    AgentReplyFrame,
    AssistantSpeechFrame,
    DisplayEventFrame,
    MotionDirection,
    RawTextFrame,
    UtteranceFrame,
)

logger = logging.getLogger(__name__)

AgentStep = str

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

PREFIX_TAG_RE = re.compile(
    r"""
    ^
    \s*
    <
      (?P<tag>emotion|speed|volume)
      \s+
      (?P<attr>value|ratio)
      \s*=\s*
      (?P<quote>["'])
      (?P<val>[^"']+)
      (?P=quote)
      \s*/\s*
    >
    """,
    re.IGNORECASE | re.VERBOSE,
)


class DisplayEmitter(Protocol):
    async def emit_display(self, event: dict[str, Any]) -> None:
        """Emit a display event matching the frontend display contract."""

    async def should_interrupt(self) -> bool:
        """Return True if current execution should stop for barge-in."""

    async def request_interrupt(self) -> None:
        """Request interruption of the currently executing action."""

    async def clear_interrupt(self) -> None:
        """Clear current interruption request state."""

    async def set_executing(self, value: bool) -> None:
        """Set execution state for interruption tracking."""

    async def register_tts_task(self, task: asyncio.Task[None]) -> None:
        """Track active TTS task so it can be interrupted."""

    async def clear_tts_task(self, task: asyncio.Task[None] | None = None) -> None:
        """Clear tracked active TTS task."""


def _build_partial_transcripts(text: str) -> list[str]:
    words = [word for word in text.split() if word]
    if len(words) <= 3:
        return [text]

    partials: list[str] = []
    for idx in range(3, len(words) + 1, 3):
        partials.append(" ".join(words[:idx]))
    if partials[-1] != text:
        partials.append(text)
    return partials


def _find_first(text: str, values: tuple[str, ...]) -> str | None:
    for value in values:
        if value in text:
            return value
    return None


def _title_case(text: str) -> str:
    return " ".join(chunk[:1].upper() + chunk[1:] for chunk in text.split() if chunk)


def _derive_target(text: str) -> str:
    colors = (
        "red",
        "blue",
        "green",
        "yellow",
        "orange",
        "purple",
        "pink",
        "white",
        "black",
        "gold",
    )
    objects = (
        "duck",
        "capsule",
        "ball",
        "plush",
        "bear",
        "toy",
        "cube",
        "box",
        "star",
        "egg",
    )
    color = _find_first(text, colors)
    obj = _find_first(text, objects)

    if color and obj:
        return _title_case(f"{color} {obj}")
    if color:
        return _title_case(color)
    if obj:
        return _title_case(obj)
    return "Front Prize"


def _derive_motions(text: str) -> list[MotionDirection]:
    motions: list[MotionDirection] = []
    if "left" in text:
        motions.append("left")
    if "right" in text:
        motions.append("right")
    if "forward" in text or "front" in text:
        motions.append("forward")
    if "back" in text or "rear" in text:
        motions.append("back")
    if "up" in text or "raise" in text:
        motions.append("up")
    if "down" in text or "drop" in text or "lower" in text:
        motions.append("down")
    if "grab" in text or "pick" in text:
        motions.append("down")

    if not motions:
        return ["right", "forward", "down"]
    return motions[:4]


def _derive_confidence(text: str, motions: list[MotionDirection]) -> float:
    confidence = 0.62
    if len(text) >= 18:
        confidence += 0.12
    if len(motions) >= 2:
        confidence += 0.10
    if any(token in text for token in ("grab", "pick", "blue", "green", "red", "duck", "capsule")):
        confidence += 0.08
    return round(min(max(confidence, 0.52), 0.97), 2)


def _parse_cartesia_prefix_tags(text: str) -> tuple[dict[str, str], str]:
    tags: dict[str, str] = {}
    rest = text

    while True:
        match = PREFIX_TAG_RE.match(rest)
        if not match:
            break

        tag = match.group("tag").lower()
        attr = match.group("attr").lower()
        value = match.group("val").strip()
        if tag == "emotion" and attr == "value":
            tags.setdefault("emotion", value)
        elif tag == "speed" and attr == "ratio":
            tags.setdefault("speed", value)
        elif tag == "volume" and attr == "ratio":
            tags.setdefault("volume", value)

        rest = rest[match.end() :]

    return tags, rest.strip()


def _stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()
    return ""


class BaseDisplayProcessor(FrameProcessor):
    def __init__(self, emitter: DisplayEmitter) -> None:
        super().__init__()
        self._emitter = emitter

    async def _emit(self, payload: dict[str, Any]) -> None:
        await self.push_frame(
            DisplayEventFrame(event=payload),
            FrameDirection.DOWNSTREAM,
        )

    async def _emit_step(self, step: AgentStep, status: str) -> None:
        await self._emit({"type": "agent_step", "step": step, "status": status})


class VoiceStartProcessor(BaseDisplayProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, (UtteranceFrame, RawTextFrame)):
            for step in AGENT_STEPS:
                await self._emit_step(step, "pending")
            if isinstance(frame, UtteranceFrame):
                await self._emit({"type": "effect", "effect": "voiceDetected"})
                await self._emit({"type": "state", "state": "listening"})

        await self.push_frame(frame, direction)


class SpeechToTextProcessor(BaseDisplayProcessor):
    def __init__(self, emitter: DisplayEmitter, partial_delay_ms: int = 180) -> None:
        super().__init__(emitter)
        self._partial_delay_s = partial_delay_ms / 1000

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, RawTextFrame):
            text = frame.text.strip()
            if not text:
                return

            await self._emit_step("speech_to_text", "active")
            await self._emit({"type": "transcript", "text": text, "isFinal": True})
            await self._emit_step("speech_to_text", "complete")
            await self.push_frame(
                TranscriptionFrame(
                    text=text,
                    user_id="display-user",
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    finalized=True,
                ),
                FrameDirection.DOWNSTREAM,
            )
            return

        if not isinstance(frame, UtteranceFrame):
            await self.push_frame(frame, direction)
            return

        text = frame.text.strip()
        if not text:
            return

        await self._emit_step("speech_to_text", "active")
        await self._emit({"type": "state", "state": "transcribing"})

        timestamp = datetime.now(tz=timezone.utc).isoformat()
        for partial in _build_partial_transcripts(text):
            await self._emit({"type": "transcript", "text": partial, "isFinal": False})
            await self.push_frame(
                InterimTranscriptionFrame(
                    text=partial,
                    user_id="display-user",
                    timestamp=timestamp,
                )
            )
            await asyncio.sleep(self._partial_delay_s)

        await self._emit({"type": "transcript", "text": text, "isFinal": True})
        await self._emit_step("speech_to_text", "complete")

        await self.push_frame(
            TranscriptionFrame(
                text=text,
                user_id="display-user",
                timestamp=timestamp,
                finalized=True,
            ),
            FrameDirection.DOWNSTREAM,
        )


class AgentProcessor(BaseDisplayProcessor):
    def __init__(
        self,
        emitter: DisplayEmitter,
        claw_controller: ClawController,
        *,
        model: str | None = None,
        max_turns: int = 6,
    ) -> None:
        super().__init__(emitter)
        self._claw_controller = claw_controller
        self._model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self._max_turns = max_turns
        self._system_prompt = (
            "You are the claw machine orchestration agent. "
            "Given user transcription text, call tools to reason and act. "
            "Always call tools in this order: parse_intent, select_target, plan_motion, execute_claw. "
            "Never mention internal mechanics like queues, pending jobs, function calls, or tool execution steps. "
            "Speak only user-facing claw actions and outcomes. "
            "After tool calls, provide one short spoken response sentence. "
            "You may prefix the response with Cartesia-compatible tags only at the start, e.g. "
            "<emotion value='excited'/><speed ratio='1.05'/>Then the sentence."
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, TranscriptionFrame):
            await self.push_frame(frame, direction)
            return

        user_text = frame.text.strip()
        if not user_text:
            return

        await self._emit({"type": "state", "state": "thinking"})
        await self._emit_step("intent_parse", "active")

        result = await self._run_turn(user_text)
        if result["interrupted"]:
            await self.push_frame(
                AgentReplyFrame(
                    text="",
                    tool_results=result["tool_results"],
                    interrupted=True,
                    reset_to_attract=False,
                )
            )
            return

        ran_execute = any(item.get("tool") == "execute_claw" for item in result["tool_results"])
        await self.push_frame(
            AgentReplyFrame(
                text=result["reply"],
                tool_results=result["tool_results"],
                interrupted=False,
                reset_to_attract=not ran_execute,
            )
        )

    def _build_agent(self, *, turn_state: dict[str, Any], tool_results: list[dict[str, Any]]) -> Any:
        @tool
        async def parse_intent(command_text: str = "") -> dict[str, Any]:
            """Parse user command into structured intent before selecting a target."""
            result = await self._tool_parse_intent(
                {"command_text": command_text},
                turn_state=turn_state,
            )
            tool_results.append({"tool": "parse_intent", "result": result})
            return result

        @tool
        async def select_target(target_hint: str = "") -> dict[str, Any]:
            """Select display target with confidence score."""
            result = await self._tool_select_target(
                {"target_hint": target_hint},
                turn_state=turn_state,
            )
            tool_results.append({"tool": "select_target", "result": result})
            return result

        @tool
        async def plan_motion(command_text: str = "", target_label: str = "") -> dict[str, Any]:
            """Generate claw motion steps from command and selected target."""
            result = await self._tool_plan_motion(
                {"command_text": command_text, "target_label": target_label},
                turn_state=turn_state,
            )
            tool_results.append({"tool": "plan_motion", "result": result})
            return result

        @tool
        async def execute_claw(target_label: str = "", motions: list[str] | None = None) -> dict[str, Any]:
            """Execute planned claw motions and return outcome."""
            result = await self._tool_execute_claw(
                {"target_label": target_label, "motions": motions},
                turn_state=turn_state,
            )
            tool_results.append({"tool": "execute_claw", "result": result})
            return result

        llm = ChatOpenAI(model=self._model, temperature=0.2)
        return create_agent(
            model=llm,
            tools=[parse_intent, select_target, plan_motion, execute_claw],
            system_prompt=self._system_prompt,
        )

    def _extract_reply(self, output: Any) -> str:
        if not isinstance(output, dict):
            return _stringify_message_content(getattr(output, "content", ""))

        messages = output.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                role = getattr(message, "type", "")
                if role == "ai":
                    text = _stringify_message_content(getattr(message, "content", ""))
                    if text:
                        return text
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = _stringify_message_content(message.get("content", ""))
                    if text:
                        return text

        output_text = output.get("output")
        if isinstance(output_text, str):
            return output_text.strip()
        return ""

    async def _run_turn(self, user_text: str) -> dict[str, Any]:
        turn_state: dict[str, Any] = {
            "user_text": user_text,
            "target_label": _derive_target(user_text.lower()),
            "motions": _derive_motions(user_text.lower()),
            "intent_complete": False,
            "target_complete": False,
            "plan_complete": False,
        }
        tool_results: list[dict[str, Any]] = []
        agent = self._build_agent(turn_state=turn_state, tool_results=tool_results)
        try:
            output = await agent.ainvoke(
                {"messages": [{"role": "user", "content": user_text}]},
                config={"recursion_limit": max(8, self._max_turns * 4)},
            )
        except Exception as exc:
            await self._emit({"type": "state", "state": "error"})
            await self._emit_step("intent_parse", "error")
            await self._emit_step("target_select", "error")
            await self._emit_step("motion_plan", "error")
            await self._emit_step("claw_execute", "error")
            await self._emit_step("result_evaluate", "error")
            return {
                "reply": "I hit a network error before I could run the claw plan.",
                "tool_results": [{"tool": "llm", "result": {"ok": False, "error": str(exc)}}],
                "interrupted": False,
            }

        interrupted = any(
            isinstance(item.get("result"), dict) and item["result"].get("interrupted")
            for item in tool_results
        )
        if interrupted:
            return {
                "reply": "",
                "tool_results": tool_results,
                "interrupted": True,
            }

        reply = self._extract_reply(output) or "On it."
        return {
            "reply": reply,
            "tool_results": tool_results,
            "interrupted": False,
        }

    async def _tool_parse_intent(self, arguments: dict[str, Any], *, turn_state: dict[str, Any]) -> dict[str, Any]:
        command_text = str(arguments.get("command_text") or turn_state["user_text"]).strip()
        lowered = command_text.lower()
        turn_state["target_label"] = _derive_target(lowered)
        turn_state["motions"] = _derive_motions(lowered)

        if not turn_state["intent_complete"]:
            turn_state["intent_complete"] = True
            await self._emit_step("intent_parse", "complete")
            await self._emit({"type": "effect", "effect": "commandParsed"})

        return {
            "ok": True,
            "command_text": command_text,
            "target_hint": turn_state["target_label"],
            "motions_hint": turn_state["motions"],
        }

    async def _tool_select_target(self, arguments: dict[str, Any], *, turn_state: dict[str, Any]) -> dict[str, Any]:
        target_hint = str(arguments.get("target_hint") or turn_state["target_label"]).strip()
        target_label = _title_case(target_hint) if target_hint else turn_state["target_label"]
        confidence = _derive_confidence(turn_state["user_text"].lower(), turn_state["motions"])
        turn_state["target_label"] = target_label

        await self._emit({"type": "state", "state": "targeting"})
        await self._emit_step("target_select", "active")
        await asyncio.sleep(0.12)
        await self._emit(
            {
                "type": "target",
                "label": target_label,
                "confidence": confidence,
            }
        )
        await self._emit_step("target_select", "complete")
        turn_state["target_complete"] = True

        return {
            "ok": True,
            "target_label": target_label,
            "confidence": confidence,
        }

    async def _tool_plan_motion(self, arguments: dict[str, Any], *, turn_state: dict[str, Any]) -> dict[str, Any]:
        command_text = str(arguments.get("command_text") or turn_state["user_text"]).strip().lower()
        target_label = str(arguments.get("target_label") or turn_state["target_label"]).strip() or turn_state["target_label"]
        motions = _derive_motions(command_text)
        turn_state["motions"] = motions
        turn_state["target_label"] = target_label

        await self._emit_step("motion_plan", "active")
        await asyncio.sleep(0.15)
        await self._emit_step("motion_plan", "complete")
        turn_state["plan_complete"] = True

        return {
            "ok": True,
            "target_label": target_label,
            "motions": motions,
            "estimated_duration_s": self._claw_controller.estimate_duration_s(motions),
        }

    async def _tool_execute_claw(self, arguments: dict[str, Any], *, turn_state: dict[str, Any]) -> dict[str, Any]:
        target_label = str(arguments.get("target_label") or turn_state["target_label"]).strip() or turn_state["target_label"]
        incoming_motions = arguments.get("motions")
        motions: list[MotionDirection]
        if isinstance(incoming_motions, list):
            safe: list[MotionDirection] = []
            for motion in incoming_motions:
                if motion in {"left", "right", "forward", "back", "down", "up"}:
                    safe.append(motion)
            motions = safe if safe else turn_state["motions"]
        else:
            motions = turn_state["motions"]

        await self._emitter.set_executing(True)
        try:
            await self._emit_step("claw_execute", "active")

            execution = await self._claw_controller.execute_plan(
                target_label=target_label,
                motions=motions,
                should_interrupt=self._emitter.should_interrupt,
                on_event=self._emit,
            )
            if execution.interrupted:
                await self._emit_step("claw_execute", "pending")
                await self._emit_step("result_evaluate", "pending")
                return {"ok": False, "interrupted": True}
            if not execution.ok:
                await self._emit_step("claw_execute", "error")
                await self._emit_step("result_evaluate", "error")
                return {
                    "ok": False,
                    "interrupted": False,
                    "error": execution.error or "controller_error",
                }

            await self._emit_step("claw_execute", "complete")
            await self._emit_step("result_evaluate", "active")
            await self._emit_step("result_evaluate", "complete")

            return {
                "ok": True,
                "interrupted": False,
                "target_label": target_label,
                "motions": motions,
                "outcome": execution.outcome,
            }
        finally:
            await self._emitter.clear_interrupt()
            await self._emitter.set_executing(False)


class TTSSpeakProcessor(BaseDisplayProcessor):
    def __init__(self, emitter: DisplayEmitter) -> None:
        super().__init__(emitter)
        self._tts_enabled = os.getenv("AGENT_TTS_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        self._cartesia_api_key = os.getenv("CARTESIA_API_KEY", "").strip()
        self._cartesia_model = os.getenv("CARTESIA_MODEL_ID", "sonic-3")
        self._cartesia_voice_id = os.getenv(
            "CARTESIA_VOICE_ID",
            "f786b574-daa5-4673-aa0c-cbe3e8534c02",
        )
        self._cartesia_language = os.getenv("CARTESIA_LANGUAGE", "en").strip() or "en"
        self._cartesia_sample_rate = int(os.getenv("CARTESIA_SAMPLE_RATE", "44100"))
        self._cartesia_client: Any | None = None
        self._audio_output = LocalAudioOutput()
        self._warned_disabled = False
        self._warned_missing_key = False
        self._warned_import = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, AssistantSpeechFrame):
            if frame.interrupted or not frame.display_text:
                await self.push_frame(frame, direction)
                return
            if self._tts_enabled:
                await self._speak(frame.tts_text)
            elif not self._warned_disabled:
                self._warned_disabled = True
                logger.warning("TTS disabled: set AGENT_TTS_ENABLED=1 to enable local Cartesia playback")
            await self._emit({"type": "emotion_clear"})
            if frame.reset_to_attract:
                await self._emit({"type": "state", "state": "attract"})

        await self.push_frame(frame, direction)

    async def _ensure_cartesia_client(self) -> Any | None:
        if not self._cartesia_api_key:
            if not self._warned_missing_key:
                self._warned_missing_key = True
                logger.warning("CARTESIA_API_KEY is not set; skipping TTS playback")
            return None
        if self._cartesia_client is not None:
            return self._cartesia_client
        try:
            from cartesia import AsyncCartesia
        except Exception as exc:
            if not self._warned_import:
                self._warned_import = True
                logger.warning("Cartesia SDK import failed; cannot play TTS: %s", exc)
            return None
        self._cartesia_client = AsyncCartesia(api_key=self._cartesia_api_key)
        return self._cartesia_client

    async def _speak(self, tagged_text: str) -> None:
        if await self._emitter.should_interrupt():
            return

        client = await self._ensure_cartesia_client()
        if client is None:
            return

        task = asyncio.create_task(self._stream_and_play(client, tagged_text))
        await self._emitter.register_tts_task(task)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            await self._emitter.clear_tts_task(task)

    async def _stream_and_play(self, client: Any, tagged_text: str) -> None:
        wrote_audio = False
        try:
            async with client.tts.websocket_connect() as connection:
                ctx = connection.context(
                    model_id=self._cartesia_model,
                    voice={"mode": "id", "id": self._cartesia_voice_id},
                    language=self._cartesia_language,
                    output_format={
                        "container": "raw",
                        "encoding": "pcm_f32le",
                        "sample_rate": self._cartesia_sample_rate,
                    },
                )
                await ctx.push(tagged_text)
                await ctx.no_more_inputs()

                async for response in ctx.receive():
                    if await self._emitter.should_interrupt():
                        break

                    event_type = getattr(response, "type", None)
                    audio = getattr(response, "audio", None)
                    if isinstance(response, dict):
                        event_type = response.get("type")
                        if audio is None:
                            audio = response.get("audio")
                        if audio is None and response.get("data"):
                            try:
                                audio = base64.b64decode(response["data"])
                            except Exception:
                                audio = None

                    if event_type == "chunk" and audio:
                        await self._audio_output.write(
                            audio,
                            sample_rate=self._cartesia_sample_rate,
                            dtype="float32",
                            channels=1,
                        )
                        wrote_audio = True
                    if event_type == "done":
                        break
                if not wrote_audio:
                    logger.warning("Cartesia TTS returned no audio chunks for text: %r", tagged_text[:80])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TTS playback failed: %s", exc)
            return
        finally:
            await self._audio_output.close()


class CartesiaMarkupProcessor(BaseDisplayProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, AgentReplyFrame):
            await self.push_frame(frame, direction)
            return

        if frame.interrupted:
            await self.push_frame(
                AssistantSpeechFrame(
                    tts_text=frame.text,
                    display_text="",
                    cartesia_emotion=None,
                    speed_ratio=None,
                    volume_ratio=None,
                    interrupted=True,
                    reset_to_attract=False,
                ),
                direction,
            )
            return

        tags, display_text = _parse_cartesia_prefix_tags(frame.text)
        emotion = tags.get("emotion")
        speed_ratio: float | None = None
        volume_ratio: float | None = None

        if tags.get("speed"):
            try:
                speed_ratio = float(tags["speed"])
            except ValueError:
                speed_ratio = None
        if tags.get("volume"):
            try:
                volume_ratio = float(tags["volume"])
            except ValueError:
                volume_ratio = None

        mood = EMOTION_MAP.get(emotion.lower()) if emotion else None
        if mood:
            payload: dict[str, Any] = {
                "type": "emotion",
                "mood": mood,
                "emotion": emotion,
            }
            if speed_ratio is not None:
                payload["speed"] = speed_ratio
            if volume_ratio is not None:
                payload["volume"] = volume_ratio
            await self._emit(payload)

        await self.push_frame(
            AssistantSpeechFrame(
                tts_text=frame.text,
                display_text=display_text or frame.text,
                cartesia_emotion=emotion,
                speed_ratio=speed_ratio,
                volume_ratio=volume_ratio,
                interrupted=False,
                reset_to_attract=frame.reset_to_attract,
            ),
            direction,
        )


class DisplayEventDispatchProcessor(FrameProcessor):
    """Sink processor that serializes DisplayEventFrame payloads to websocket clients."""

    def __init__(self, emitter: DisplayEmitter) -> None:
        super().__init__()
        self._emitter = emitter

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, DisplayEventFrame):
            await self._emitter.emit_display(frame.event)
            return

        await self.push_frame(frame, direction)
