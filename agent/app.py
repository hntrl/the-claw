from __future__ import annotations

import asyncio
from enum import Enum
import json
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

load_dotenv()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

SYSTEM_PROMPT = """
You are ClawPilot, a voice agent that controls a real claw machine.

Personality:
- Playfully sassy and theatrical.
- Keep it witty but never cruel.
- Keep spoken responses short (1-2 sentences).

Rules:
- Use incremental movement by default.
- If user asks to stop, call emergency_stop immediately.
- Never invent machine state. Use get_state when uncertain.
- Favor safety: do not issue long or repeated risky motions.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "Move the claw in one direction for a short duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["left", "right", "forward", "backward"],
                    },
                    "duration_ms": {"type": "integer", "minimum": 100, "maximum": 1200},
                },
                "required": ["direction", "duration_ms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop",
            "description": "Drop the claw once.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "home",
            "description": "Return the claw to home/origin position.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Get current machine state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "emergency_stop",
            "description": "Immediately stop all motion and enter safe mode.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class Direction(str, Enum):
    left = "left"
    right = "right"
    forward = "forward"
    backward = "backward"


class MoveRequest(BaseModel):
    direction: Direction
    duration_ms: int = Field(ge=100, le=1200)


class Limits(BaseModel):
    x_min: int = 0
    x_max: int = 100
    y_min: int = 0
    y_max: int = 100


class ClawState(BaseModel):
    is_busy: bool
    x: int
    y: int
    limits: Limits
    last_error: str | None
    emergency_stopped: bool
    last_command_at: float | None


class TurnRequest(BaseModel):
    text: str = Field(min_length=1)
    session_id: str = "default"


class TurnResponse(BaseModel):
    session_id: str
    reply: str
    tool_events: list[dict[str, Any]]


class TranscribeResponse(BaseModel):
    text: str


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)


class ClawController:
    def __init__(self) -> None:
        self.limits = Limits()
        self.x = 50
        self.y = 50
        self.is_busy = False
        self.last_error: str | None = None
        self.emergency_stopped = False
        self.last_command_at: float | None = None
        self._lock = asyncio.Lock()

    def state(self) -> ClawState:
        return ClawState(
            is_busy=self.is_busy,
            x=self.x,
            y=self.y,
            limits=self.limits,
            last_error=self.last_error,
            emergency_stopped=self.emergency_stopped,
            last_command_at=self.last_command_at,
        )

    async def _cooldown(self) -> None:
        if self.last_command_at is None:
            return
        elapsed = time.monotonic() - self.last_command_at
        remaining = 0.2 - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def move(self, req: MoveRequest) -> dict[str, Any]:
        if self.emergency_stopped:
            raise HTTPException(status_code=409, detail="Controller is emergency stopped")
        async with self._lock:
            await self._cooldown()
            self.is_busy = True
            self.last_error = None
            self.last_command_at = time.monotonic()
            await asyncio.sleep(req.duration_ms / 1000)
            step = max(1, int(req.duration_ms / 100))

            if req.direction == Direction.left:
                self.x = max(self.limits.x_min, self.x - step)
            elif req.direction == Direction.right:
                self.x = min(self.limits.x_max, self.x + step)
            elif req.direction == Direction.forward:
                self.y = min(self.limits.y_max, self.y + step)
            elif req.direction == Direction.backward:
                self.y = max(self.limits.y_min, self.y - step)

            self.is_busy = False
            return {"ok": True, "state": self.state().model_dump()}

    async def drop(self) -> dict[str, Any]:
        if self.emergency_stopped:
            raise HTTPException(status_code=409, detail="Controller is emergency stopped")
        async with self._lock:
            await self._cooldown()
            self.is_busy = True
            self.last_error = None
            self.last_command_at = time.monotonic()
            await asyncio.sleep(1.5)
            self.is_busy = False
            return {"ok": True, "dropped": True, "state": self.state().model_dump()}

    async def home(self) -> dict[str, Any]:
        if self.emergency_stopped:
            raise HTTPException(status_code=409, detail="Controller is emergency stopped")
        async with self._lock:
            await self._cooldown()
            self.is_busy = True
            self.last_error = None
            self.last_command_at = time.monotonic()
            await asyncio.sleep(1.0)
            self.x = 0
            self.y = 0
            self.is_busy = False
            return {"ok": True, "homed": True, "state": self.state().model_dump()}

    async def emergency_stop(self) -> dict[str, Any]:
        self.emergency_stopped = True
        self.is_busy = False
        self.last_command_at = time.monotonic()
        return {"ok": True, "stopped": True, "state": self.state().model_dump()}

    async def reset_emergency(self) -> dict[str, Any]:
        self.emergency_stopped = False
        self.last_command_at = time.monotonic()
        return {"ok": True, "reset": True, "state": self.state().model_dump()}


app = FastAPI(title="Claw Demo Unified App", version="0.2.0")
client = OpenAI()
controller = ClawController()


async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "move":
            direction = args.get("direction")
            duration_ms = int(args.get("duration_ms", 250))
            duration_ms = max(100, min(1200, duration_ms))
            return await controller.move(MoveRequest(direction=direction, duration_ms=duration_ms))
        if name == "drop":
            return await controller.drop()
        if name == "home":
            return await controller.home()
        if name == "get_state":
            return controller.state().model_dump()
        if name == "emergency_stop":
            return await controller.emergency_stop()
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"Unknown tool: {name}"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def web_index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    return controller.state().model_dump()


@app.post("/api/emergency-stop")
async def api_emergency_stop() -> dict[str, Any]:
    return await controller.emergency_stop()


@app.post("/api/reset-emergency")
async def api_reset_emergency() -> dict[str, Any]:
    return await controller.reset_emergency()


# Preserve bridge-compatible endpoints so existing scripts and operator habits keep working.
@app.get("/state")
async def bridge_state() -> dict[str, Any]:
    return controller.state().model_dump()


@app.post("/cmd/move")
async def bridge_move(req: MoveRequest) -> dict[str, Any]:
    return await controller.move(req)


@app.post("/cmd/drop")
async def bridge_drop() -> dict[str, Any]:
    return await controller.drop()


@app.post("/cmd/home")
async def bridge_home() -> dict[str, Any]:
    return await controller.home()


@app.post("/cmd/stop")
async def bridge_stop() -> dict[str, Any]:
    return await controller.emergency_stop()


@app.post("/cmd/reset-emergency")
async def bridge_reset_emergency() -> dict[str, Any]:
    return await controller.reset_emergency()


@app.post("/turn", response_model=TurnResponse)
async def turn(req: TurnRequest) -> TurnResponse:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": req.text},
    ]
    tool_events: list[dict[str, Any]] = []

    for _ in range(4):
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        message = completion.choices[0].message
        assistant_text = message.content or ""
        tool_calls = message.tool_calls or []
        if not tool_calls:
            if assistant_text:
                messages.append({"role": "assistant", "content": assistant_text})
            return TurnResponse(
                session_id=req.session_id,
                reply=assistant_text.strip() or "Ready for your next move.",
                tool_events=tool_events,
            )

        # Tool messages must follow an assistant message that carries tool_calls.
        messages.append(
            {
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            result = await call_tool(name, args)
            tool_events.append({"tool": name, "args": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": name,
                    "content": json.dumps(result),
                }
            )

    return TurnResponse(
        session_id=req.session_id,
        reply="I hit my planning limit for this turn. Ask for one action at a time.",
        tool_events=tool_events,
    )


@app.post("/api/transcribe", response_model=TranscribeResponse)
async def api_transcribe(audio: UploadFile = File(...)) -> TranscribeResponse:
    blob = await audio.read()
    if not blob:
        raise HTTPException(status_code=400, detail="Empty audio payload")

    try:
        transcription = await asyncio.to_thread(
            lambda: client.with_options(timeout=20.0).audio.transcriptions.create(
                model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
                file=(audio.filename or "mic.webm", blob, audio.content_type or "audio/webm"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}") from exc

    text = (transcription.text or "").strip()
    return TranscribeResponse(text=text)


async def synthesize_cartesia(text: str) -> tuple[bytes, str]:
    api_key = os.getenv("CARTESIA_API_KEY", "").strip()
    voice_id = os.getenv("CARTESIA_VOICE_ID", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="CARTESIA_API_KEY is not set")
    if not voice_id:
        raise HTTPException(status_code=400, detail="CARTESIA_VOICE_ID is not set")

    payload: dict[str, Any] = {
        "model_id": os.getenv("CARTESIA_MODEL_ID", "sonic-2"),
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "mp3",
            "sample_rate": int(os.getenv("CARTESIA_SAMPLE_RATE", "44100")),
            "bit_rate": int(os.getenv("CARTESIA_MP3_BIT_RATE", "128000")),
        },
        "language": os.getenv("CARTESIA_LANGUAGE", "en"),
    }
    generation_speed = os.getenv("CARTESIA_GENERATION_SPEED")
    if generation_speed:
        try:
            payload["generation_config"] = {"speed": float(generation_speed)}
        except ValueError:
            pass

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Cartesia-Version": os.getenv("CARTESIA_VERSION", "2025-04-16"),
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(25.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post("https://api.cartesia.ai/tts/bytes", headers=headers, json=payload)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Cartesia TTS failed: {resp.text}")
    return resp.content, "audio/mpeg"


@app.post("/api/tts")
async def api_tts(req: TTSRequest):
    provider = os.getenv("TTS_PROVIDER", "cartesia").lower()
    if provider == "none":
        raise HTTPException(status_code=400, detail="TTS provider disabled")
    if provider != "cartesia":
        raise HTTPException(status_code=400, detail=f"Unsupported TTS_PROVIDER: {provider}")

    audio_bytes, content_type = await synthesize_cartesia(req.text)
    return Response(content=audio_bytes, media_type=content_type)


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
