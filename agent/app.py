"""
Voice-controlled claw machine agent — wired to the V2_agent firmware via
claw_controller.ClawController.

Key changes from the upstream demo:
- ClawController is now the real hardware-backed async wrapper, not a sim.
- Tool schema exposes degree-based motion on X/Y/Z and explicit servo control,
  matching what the firmware actually does.
- System prompt teaches the LLM about the physical machine: degree units,
  servo limits, Z-axis semantics, limit switches, when to home.
- Startup opens the serial port; shutdown closes it cleanly.
- Legacy /cmd/* and /api/* endpoints kept as shims so the existing web UI
  (index.html, app.js) continues to work without changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

from .claw_controller import ClawController

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("claw_app")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

# ============================================================================
# EDIT HERE: per-axis degrees-per-foot calibration.
#
# These constants are used in the system prompt to teach the LLM how to
# translate physical distances ("move forward a foot") into degree counts
# for the firmware. They are NOT used directly in any conversion code —
# they're rendered into the system prompt below so the LLM has accurate
# numbers in front of it when it picks tool arguments.
#
# Why per-axis: X, Y, and Z use different mechanical drivetrains (belt for
# X/Y, cable spool for Z), so the same motor degree count corresponds to
# different physical distances on each axis.
#
# CALIBRATION NOTES:
#   Z is a cable spool: 360 degrees of motor rotation reels in/out roughly
#   12 inches of cable at the current spool diameter. This is the original
#   tuning Ben measured on the spool.
#
#   X and Y are belt-driven gantries. Empirically, asking for "1 foot" of
#   X/Y motion using the Z calibration (360 deg/foot) produced about 8
#   inches of physical movement — i.e., 360 deg covers ~8 inches, so to
#   get 12 inches you need 360 * (12/8) = 540 deg/foot.
#
# These are STARTING POINTS. Measure actual displacement after a known move
# and adjust. The LLM will use whatever values are baked into the system
# prompt, so changes here take effect on the next agent restart.
# ============================================================================
X_DEGREES_PER_FOOT = 540.0   # ~45 deg/inch on the X gantry
Y_DEGREES_PER_FOOT = 540.0   # assumed same belt geometry as X; adjust if Y differs
Z_DEGREES_PER_FOOT = 360.0   # cable spool — 1 rev ≈ 1 foot of cable

# ============================================================================
# EDIT HERE: default depth for unspecified "lower the claw" requests.
#
# When the user says "lower the claw" / "drop down" / "go to grab" without
# specifying a distance, the agent calls lower_claw() with no argument and
# this default is used. The user can always ask for further descent — the
# firmware enforces a hard MAX of 6 feet of cable below the locked Z=0
# ceiling.
#
# Examples that go beyond this default:
#   "lower the claw"               -> 1080 degrees down (3 feet)
#   "lower it 5 feet"              -> agent calls lower_claw(degrees=1800)
#   "lower it" then "go down more" -> two stacked descents stack normally
#                                     up to the safety bound.
# ============================================================================
DEFAULT_LOWER_DEGREES = Z_DEGREES_PER_FOOT * 3  # 3 feet = 1080 degrees

# ----------------------------------------------------------------------------
# System prompt — teaches the LLM about the actual machine
# ----------------------------------------------------------------------------

# Single system prompt — there is no longer a phased setup mode. The Z top
# limit switch is the source of truth for Z=0, so any reasoning about
# "ceiling locked" or "init phase" is gone. The agent's only Z-related
# concern is whether Z has been homed at least once this power cycle
# (z_homed); that's surfaced through the per-turn status banner.

SYSTEM_PROMPT_TEMPLATE = """You are ClawPilot, a voice agent for a DIY claw machine.

Personality: playfully sassy, theatrical, brief (1-2 sentences).

Machine basics:
- Three axes: X (gantry forward/back), Y (left/right), Z (claw up/down).
- Motion uses DEGREES of motor rotation. The mapping differs per axis:
    X: {X_DEGREES_PER_FOOT:.0f} deg ≈ 1 foot of gantry travel
    Y: {Y_DEGREES_PER_FOOT:.0f} deg ≈ 1 foot of gantry travel
    Z: {Z_DEGREES_PER_FOOT:.0f} deg ≈ 1 foot of cable
  Always use the per-axis value — don't apply Z's calibration to X or Y.
- Sign conventions:
    X positive = FORWARD (toward operator); X negative = backward
    Y positive = RIGHT;  Y negative = LEFT
    Z positive = DOWN;   Z negative = UP
- Servo: open=90, closed=25. Limits clamped to [25, 90].
- Limit switches at travel extremes auto-stop motion. Status LIMIT means
  the axis bumped a switch.

Z top limit switch:
- The Z top limit switch is hardware. Whenever Z retracts and the switch
  fires, the firmware auto-resyncs Z=0. This is robust against the
  cable-spool tension shocks that used to cause skipped steps and
  position drift.
- z_homed=False means Z has not yet hit the switch this power cycle —
  reported Z values are best-effort guesses. Suggest the user start with
  "raise the claw" or "home the claw" to establish a real Z reference.
- z_homed=True means Z=0 is reliable.
- Z safety bottom: 6 feet of cable below the top. Out-of-range descents
  are clamped silently — the result reflects actual motion.

Tools and how to use them:
- move_axis(axis, degrees): single-axis move with signed degrees.
- lower_claw(): drop the claw 3 feet (default).
- lower_claw(degrees=N): drop by a specific amount. "Lower it 4 feet" -> 1440.
- raise_claw(): retract the claw to the top limit (resyncs Z=0).
- home_z(): same as raise_claw — drives up to the Z top limit. Use when
  you want to resync Z without the full X/Y home sweep.
- open_claw(angle?): open. Default 90.
- close_claw(angle?): close. Default 25.
- home(): full homing sequence — Z up to limit, then X+A home and back
  off, then Y homes and backs off a quarter turn, then claw opens. Parks
  at the front-left dropoff corner. ALSO serves as delivery. Call when:
    - User wants to deliver the prize ("bring it to me", "drop it off")
    - User wants to recalibrate / re-home
  Note: home ALWAYS opens the claw at the end. If the user is mid-grab and
  asks to "recalibrate" rather than deliver, briefly warn that this will
  release the prize.
  home() and home_z() can fail with status STUCK if a motor doesn't reach
  its limit switch within 20 seconds — surface that to the user as
  "looks like a motor's stuck" and stop chaining further moves.
- halt(): EMERGENCY STOP. Call FIRST on "stop", "wait", "that's enough".
- reset_emergency(): clear the halt latch.
- get_state(): inspect position, limit switches, servo angle, z_homed.

Rules:
- Prefer small incremental moves. Never chain more than 2-3 moves per turn.
- A "grab" or "pick up" = lower_claw(), close_claw(), raise_claw().
  After a successful grab, call home() if the user asked to receive the prize.
- If a tool returns status LIMIT, mention it briefly.
- If a tool returns status STUCK (homing failure), tell the user a motor
  appears stuck and stop. Don't retry without their say-so.
- If z_homed is False at the start of a session and the user asks for
  anything that depends on Z position (lower, grab, deliver), suggest
  homing Z first.
"""

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(
    X_DEGREES_PER_FOOT=X_DEGREES_PER_FOOT,
    Y_DEGREES_PER_FOOT=Y_DEGREES_PER_FOOT,
    Z_DEGREES_PER_FOOT=Z_DEGREES_PER_FOOT,
)




# ----------------------------------------------------------------------------
# Tool schema — matches the firmware
# ----------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "move_axis",
            "description": "Move one axis of the claw by a signed number of degrees. "
                           "Positive X = forward (toward operator), negative X = backward. "
                           "Positive Y = right, negative Y = left. "
                           "Positive Z = down, negative Z = up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "axis": {"type": "string", "enum": ["x", "y", "z"]},
                    "degrees": {
                        "type": "number",
                        "description": "Signed degrees. ~30-90 = small nudge, ~180 = medium, 360+ = large.",
                    },
                },
                "required": ["axis", "degrees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_claw",
            "description": "Open the claw servo. Optional angle (25-90). Default fully open.",
            "parameters": {
                "type": "object",
                "properties": {
                    "angle": {"type": "integer", "minimum": 25, "maximum": 90},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lower_claw",
            "description": "Lower the claw. With no argument, lowers by the default depth "
                           "(approximately 3 feet of cable, 1080 degrees). Pass `degrees` to "
                           "override — e.g. for 'lower it 4 feet' use degrees = 1440. The "
                           "firmware enforces a 6-foot maximum descent (Z_MAX_DOWN_DEGREES = "
                           "2160) and silently clamps deeper requests. For very small "
                           "adjustments, prefer move_axis(z, ...) instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "degrees": {
                        "type": "number",
                        "description": "Optional. Positive degrees to descend. ~360 = 1 foot, "
                                       "~720 = 2 feet, ~1080 = 3 feet (the default), "
                                       "~1440 = 4 feet. Omit for the default 3-foot drop.",
                        "minimum": 0,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "raise_claw",
            "description": "Retract the claw all the way back up to the top (Z=0). Use "
                           "whenever the user says 'raise', 'come back up', 'lift', or similar.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_claw",
            "description": "Close the claw servo. Optional angle (25-90). Default fully closed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "angle": {"type": "integer", "minimum": 25, "maximum": 90},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "home_z",
            "description": "Drive the claw upward until the Z top limit switch fires. "
                           "Resyncs Z=0 mechanically, regardless of any drift in step counting. "
                           "Use this when the user says 'raise the claw' / 'bring it up' / "
                           "'go to the top', or when you suspect Z position has drifted from "
                           "physical reality (cable spool tension shocks can cause skipped steps). "
                           "Faster than home() because it skips the X/Y phases. "
                           "Can fail with status STUCK if the switch never fires within 20 seconds.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "home",
            "description": "Run the full homing sequence: Z drives up to its top limit "
                           "(resyncs Z=0), then X gantry homes and backs off, then Y homes "
                           "and backs off a quarter turn, then the claw opens. Parks at the "
                           "front-left dropoff corner, so this also serves as the delivery "
                           "action — call it when the user wants to recalibrate OR when they "
                           "say 'bring it to me' / 'drop it off' / 'deliver it'. Note: the "
                           "claw ALWAYS opens at the end, so warn the user first if homing "
                           "while gripping a prize for non-delivery reasons. Can fail with "
                           "status STUCK if any motor doesn't reach its limit within 20 seconds.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Get the current machine state: positions, limit switches, servo angle.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "halt",
            "description": "EMERGENCY stop. Halts all motion immediately. Use if the user says "
                           "stop, wait, or anything urgent. Machine stays latched until reset.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_emergency",
            "description": "Clear the emergency-stop latch so motion can resume after a halt.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ----------------------------------------------------------------------------
# Request/response models
# ----------------------------------------------------------------------------


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


# Legacy shim request model — kept so the existing web UI keeps working.
class LegacyMoveRequest(BaseModel):
    direction: str  # left/right/forward/backward — translated to axis + sign
    duration_ms: int = Field(ge=100, le=1200)


# ----------------------------------------------------------------------------
# App setup with lifespan for hardware init / teardown
# ----------------------------------------------------------------------------

controller = ClawController()
client = OpenAI()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting ClawController (port=%s, sim=%s)", controller.port, controller._sim)
    await controller.start()
    try:
        yield
    finally:
        log.info("Shutting down ClawController")
        await controller.close()


app = FastAPI(title="Claw Demo — Hardware", version="0.3.0", lifespan=lifespan)


# ----------------------------------------------------------------------------
# Tool dispatch — maps LLM tool calls to ClawController methods
# ----------------------------------------------------------------------------


async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "move_axis":
            axis = str(args.get("axis", "")).lower()
            degrees = float(args.get("degrees", 0))
            result = await controller.move_axis(axis, degrees)
            return _result_to_dict(result)

        if name == "open_claw":
            angle = args.get("angle")
            result = await controller.open_claw(int(angle) if angle is not None else None)
            return _result_to_dict(result)

        if name == "lower_claw":
            # Soft default depth — user can request more via subsequent move_axis(z) calls.
            degrees = args.get("degrees", DEFAULT_LOWER_DEGREES)
            result = await controller.lower_claw(float(degrees))
            return _result_to_dict(result)

        if name == "raise_claw":
            result = await controller.raise_claw()
            return _result_to_dict(result)

        if name == "close_claw":
            angle = args.get("angle")
            result = await controller.close_claw(int(angle) if angle is not None else None)
            return _result_to_dict(result)

        if name == "home":
            result = await controller.home()
            return _result_to_dict(result)

        if name == "home_z":
            result = await controller.home_z()
            return _result_to_dict(result)

        if name == "get_state":
            state = controller.state()
            return {"ok": True, "state": state.model_dump()}

        if name == "halt":
            result = await controller.halt()
            return _result_to_dict(result)

        if name == "reset_emergency":
            result = await controller.reset_emergency()
            return _result_to_dict(result)
    except Exception as exc:  # noqa: BLE001
        log.exception("Tool dispatch error for %s", name)
        return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"Unknown tool: {name}"}


def _result_to_dict(result) -> dict[str, Any]:
    d = {
        "ok": result.ok,
        "status": result.status,
        "command": result.command,
    }
    if result.error:
        d["error"] = result.error
    if result.events:
        d["events"] = result.events
    # Include a compact state snapshot so the LLM's follow-up has context.
    d["state"] = controller.state().model_dump()
    return d


# ----------------------------------------------------------------------------
# Health / static / root
# ----------------------------------------------------------------------------


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
    result = await controller.halt()
    return _result_to_dict(result)


@app.post("/api/reset-emergency")
async def api_reset_emergency() -> dict[str, Any]:
    result = await controller.reset_emergency()
    return _result_to_dict(result)


# ----------------------------------------------------------------------------
# Legacy /cmd/* shims — preserve compatibility with existing web UI.
# The original UI has buttons that POST to these endpoints; we translate them
# into real hardware calls. Direction-based move becomes a modest degree move.
# ----------------------------------------------------------------------------


@app.get("/state")
async def bridge_state() -> dict[str, Any]:
    return controller.state().model_dump()


@app.post("/cmd/move")
async def bridge_move(req: LegacyMoveRequest) -> dict[str, Any]:
    # Translate direction+duration into an axis+degree move. The mapping here
    # is crude (duration -> degree count) and exists only so the old buttons
    # don't 500 — real motion should go through /turn or /cmd/* with degrees.
    # Convention: X = forward/back, Y = left/right.
    degrees_per_ms = 0.4  # tune as needed
    magnitude = req.duration_ms * degrees_per_ms
    mapping = {
        "forward":  ("x",  magnitude),
        "backward": ("x", -magnitude),
        "right":    ("y",  magnitude),
        "left":     ("y", -magnitude),
    }
    if req.direction not in mapping:
        raise HTTPException(status_code=400, detail=f"bad direction {req.direction}")
    axis, degrees = mapping[req.direction]
    result = await controller.move_axis(axis, degrees)
    return _result_to_dict(result)


@app.post("/cmd/drop")
async def bridge_drop() -> dict[str, Any]:
    # Best-effort "drop" composite: Z down, close claw, Z up.
    # The agent will usually do this more intelligently via /turn.
    r1 = await controller.move_axis("z", 720)     # lower
    r2 = await controller.close_claw()
    r3 = await controller.move_axis("z", -720)    # raise
    return {
        "ok": all(r.ok for r in (r1, r2, r3)),
        "state": controller.state().model_dump(),
        "steps": [_result_to_dict(r) for r in (r1, r2, r3)],
    }


@app.post("/cmd/home")
async def bridge_home() -> dict[str, Any]:
    result = await controller.home()
    return _result_to_dict(result)


@app.post("/cmd/stop")
async def bridge_stop() -> dict[str, Any]:
    result = await controller.halt()
    return _result_to_dict(result)


@app.post("/cmd/reset-emergency")
async def bridge_reset_emergency() -> dict[str, Any]:
    result = await controller.reset_emergency()
    return _result_to_dict(result)


# ----------------------------------------------------------------------------
# Core agent turn
# ----------------------------------------------------------------------------


@app.post("/turn", response_model=TurnResponse)
async def turn(req: TurnRequest) -> TurnResponse:
    # Snapshot machine state ONCE at the start of the turn. We inject a
    # one-line authoritative status banner into the user message so the LLM
    # has fresh, accurate state every turn (rather than reasoning from
    # potentially stale conversation history).
    snap = controller.state()
    fsm = snap.fsm_state
    z_homed_str = "YES" if snap.z_homed else "NO (Z position is a guess until homed)"
    status_banner = (
        f"[machine status: fsm={fsm}, Z={snap.z}, Z_homed={z_homed_str}]"
    )

    user_content = f"{status_banner}\n{req.text}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
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
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
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


# ----------------------------------------------------------------------------
# Transcription and TTS — unchanged from upstream
# ----------------------------------------------------------------------------


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
