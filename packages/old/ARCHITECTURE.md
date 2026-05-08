# Claw Machine Architecture & Protocol Reference

This document supplements the README. It describes the end-to-end pipeline from
voice input to physical motor motion, the function inventory at each layer,
and the exact wire protocol between the host computer and the Arduino.

A new engineer reading this should be able to:
1. Trace a single user utterance from microphone to stepper motor.
2. Modify any layer without breaking the others.
3. Send hand-crafted commands directly to the firmware over a serial terminal
   for debugging.

---

## 1. System Overview

Three processes cooperate:

```
┌──────────────────────────┐    HTTP    ┌──────────────────────────┐    USB     ┌──────────────────────────┐
│  Browser (index.html /   │  ────────► │  FastAPI server (app.py) │  ────────► │  Arduino Uno + CNC       │
│  app.js)                 │  ◄──────── │  + ClawController        │  ◄──────── │  Shield V3 + A4988s      │
│  – mic capture           │  WebSocket │  – LLM agent loop        │   serial   │  – stepper drivers       │
│  – TTS playback          │   not used │  – tool dispatch         │   115200   │  – limit switches        │
│  – state UI              │            │  – serial framing        │            │  – servo                 │
└──────────────────────────┘            └──────────────────────────┘            └──────────────────────────┘
```

The browser is presentation-only. All logic lives in `app.py`. The firmware
is a state machine that accepts text commands over USB serial and emits
text responses on the same line.

---

## 2. The Pipeline (Voice → Motion)

### 2.1 Audio capture (browser, `app.js`)

The browser opens a microphone stream via `getUserMedia` and runs a small
voice-activity detector (VAD) on the audio. When speech is detected, audio
chunks are accumulated in a `MediaRecorder`. When silence resumes (or the
operator releases push-to-talk), the recorder stops and the WebM blob is
POSTed to `/api/transcribe`.

Push-to-talk uses the `m` key by default. The page also exposes a "Force
Record" button for short utterances and a manual text input as a fallback.

### 2.2 Transcription (`app.py:/api/transcribe`)

The endpoint hands the audio blob to OpenAI's audio API
(`gpt-4o-mini-transcribe` by default) and returns the recognized text. No
state is kept here.

### 2.3 Agent turn (`app.py:/turn`)

The browser POSTs `{text, session_id}`. The handler:

1. Snapshots the current machine state (`controller.state()`).
2. Builds a one-line **status banner** prepended to the user message:
   `[machine status: fsm=<state>, Z=<steps>, Z_homed=YES|NO]`.
   This injects authoritative state into every turn so the LLM cannot
   drift based on stale conversation history.
3. Calls the OpenAI Chat Completions API with a fixed `SYSTEM_PROMPT`,
   the static `TOOLS` list, and the user message.
4. Loops up to 4 times executing whatever tools the LLM calls.
5. Returns `{reply, tool_events}` to the browser.

The `tool_events` array surfaces every tool call and its result so the UI
can show a debug log.

### 2.4 Tool dispatch (`app.py:call_tool`)

Each tool name maps to a method on the singleton `ClawController`:

| Tool name         | Calls                                       |
|-------------------|---------------------------------------------|
| `move_axis`       | `controller.move_axis(axis, degrees)`       |
| `lower_claw`      | `controller.lower_claw(degrees)` (default 1080) |
| `raise_claw`      | `controller.raise_claw()` (sends RAISE)     |
| `open_claw`       | `controller.open_claw(angle)`               |
| `close_claw`      | `controller.close_claw(angle)`              |
| `home`            | `controller.home()`                         |
| `home_z`          | `controller.home_z()`                       |
| `halt`            | `controller.halt()`                         |
| `reset_emergency` | `controller.reset_emergency()`              |
| `get_state`       | `controller.state()`                        |

Each method returns a `CommandResult` (or, for `get_state`, a `ClawState`
snapshot) which is serialized to JSON and fed back to the LLM as the tool
result.

### 2.5 Controller (`claw_controller.py`)

`ClawController` owns the serial port. It runs a background reader thread
that parses every line from the firmware and:

- Resolves pending `asyncio.Future`s when `DONE`, `ERR`, or `PONG` arrive.
- Updates the cached `ClawState` from `STATE` and `EVT` lines.

Each `async def` motion method (`move_axis`, `home`, etc.) calls the private
`_send(command)` helper, which:

1. Mints a numeric command id.
2. Prefixes the command with `#<id> ` and writes it to the serial port.
3. Awaits the matching `DONE` or `ERR` line, with a timeout.
4. Returns a `CommandResult`.

This means motion methods are **blocking from the agent's perspective**:
calling `await controller.home()` returns only when the firmware says HOME
is done (or fails). The agent loop naturally serializes commands.

A simulation mode (`CLAW_SIM=1`) skips the serial port and updates the
cached state directly. Useful for testing the agent without hardware.

### 2.6 Firmware (`claw_machine_V4_agent.ino`)

The main loop polls serial input every iteration (so HALT/STATE?/PING work
mid-motion) and runs a state machine:

```
IDLE  ──[X /Y /Z ]──►  MOVING_X / MOVING_Y / MOVING_Z  ──►  IDLE
      ──[HOME]──►  HOMING_Z_TO_LIMIT  ──►  HOMING_TO_LIMIT  ──►  HOMING_WAIT
                  ──►  HOMING_BACKOFF  ──►  HOMING_Y_TO_LIMIT
                  ──►  HOMING_Y_BACKOFF  ──►  HOMING_OPEN_CLAW  ──►  IDLE
      ──[ZHOME]──►  HOMING_Z_TO_LIMIT  ──►  IDLE
```

Each tick calls `stepper.run()` on whichever AccelStepper instance is
active for the current state. Limit switches are polled with debouncing
(must read LOW for `LIMIT_DEBOUNCE_READS = 3` consecutive ticks before
counting as a hit).

---

## 3. Wire Protocol (Host ↔ Firmware)

This is the protocol you use if you `screen /dev/ttyACM0 115200` and type
commands by hand.

### 3.1 General format

- Communication is line-oriented. Lines are terminated by `\n`.
- Baud rate: **115200**.
- All commands and responses are ASCII.
- Each command may be prefixed with `#<id> ` where `<id>` is any string
  without whitespace. The firmware echoes the id in `ACK`, `DONE`, and
  `ERR` lines so the host can correlate. If you omit the prefix, the
  firmware uses `-` as the id.

Example exchange:

```
host:     #42 X 90
firmware: ACK 42 X
firmware: EVT MOVING axis=X degrees=90.00 steps=155
firmware: DONE 42 X OK
```

### 3.2 Commands

The complete list of commands the firmware accepts:

| Command          | Args              | Description |
|------------------|-------------------|-------------|
| `X <degrees>`    | float             | Move X gantry by signed degrees of motor rotation. Sign convention is mechanical — sign of X positive depends on motor wiring; agent treats +X as "forward". |
| `Y <degrees>`    | float             | Move Y gantry. Same convention. |
| `Z <degrees>`    | float             | Move Z. **Positive = down** (claw descends), **negative = up** (claw retracts). Target is software-clamped to `[0, Z_MAX_DOWN_DEGREES]`. The Z top limit switch may auto-stop upward motion before the software target is reached and resync `Z=0`. |
| `ZHOME`          | (none)            | Drive Z upward until the top limit switch fires; resync `Z=0`. Times out after 20 s with `DONE ZHOME STUCK`. |
| `RAISE`          | (none)            | Alias for `ZHOME`. |
| `HOME`           | (none)            | Full homing sequence: Z up, then X+A, then Y, then claw open. See §3.5. |
| `OPEN [angle]`   | int 25–90         | Open claw servo. Default 90. |
| `CLOSE [angle]`  | int 25–90         | Close claw servo. Default 25. |
| `S <angle>`      | int 25–90         | Set servo to absolute angle. (Also accepts `SERVO`.) |
| `HALT`           | (none)            | Immediately stop all motion. Aborts any in-progress motion command. |
| `STATE?`         | (none)            | Emit a one-line state snapshot. (Also accepts `STATE`.) |
| `PING`           | (none)            | Heartbeat. Firmware replies with `PONG <id>`. |
| `EN`             | (none)            | Enable motor drivers (active LOW on the shield's EN pin). |
| `DIS`            | (none)            | Disable motor drivers. **Warning: gantry will fall under gravity.** |

### 3.3 Response types

The firmware emits one of these line types. The first whitespace-delimited
token always identifies the type:

| Prefix    | Format                                         | Meaning |
|-----------|------------------------------------------------|---------|
| `READY`   | `READY`                                        | Boot complete. |
| `INFO`    | `INFO <text>`                                  | Free-form informational. |
| `ACK`     | `ACK <id> <cmd>`                               | Command accepted, motion starting. |
| `EVT`     | `EVT <event> [k=v ...]`                        | Mid-motion event (limit hit, clamp, etc.). |
| `DONE`    | `DONE <id> <cmd> <status>`                     | Command complete. Status: `OK`, `LIMIT`, `HALTED`, `STUCK`. |
| `ERR`     | `ERR <id> <reason>`                            | Command rejected. Reason: `BUSY`, `UNKNOWN`, `BAD_ARGS`. |
| `PONG`    | `PONG <id>`                                    | Reply to `PING`. |
| `STATE`   | `STATE <id> <fsm> <key=value ...>`             | Reply to `STATE?`. See §3.4. |

DONE statuses in detail:
- `OK` — clean completion at the commanded target.
- `LIMIT` — completed by hitting a limit switch (e.g. an X move that ran
  into the X+ switch). Not an error; the agent uses this to know the
  travel ended early.
- `HALTED` — aborted by a `HALT` command.
- `STUCK` — homing phase timed out (motor presumably slipping or jammed).

### 3.4 STATE format

```
STATE <id> <FSM_STATE> X=<steps> Y=<steps> Z=<agent_steps> ZMAX=<steps> ZREF=<steps>
      ZHOMED=<0|1> LX=<0|1> LA=<0|1> LY=<0|1> LZTOP=<0|1> SERVO=<angle> EN=<0|1>
```

- `FSM_STATE` — one of `IDLE`, `MOVING_X`, `MOVING_Y`, `MOVING_Z`,
  `BACKING_OFF_X`, `BACKING_OFF_Y`, `HOMING_Z_TO_LIMIT`, `HOMING_TO_LIMIT`,
  `HOMING_WAIT`, `HOMING_BACKOFF`, `HOMING_Y_TO_LIMIT`,
  `HOMING_Y_BACKOFF`, `HOMING_OPEN_CLAW`.
- `X`, `Y` — raw stepper position counters in motor steps.
- `Z` — agent-frame Z position in motor steps. 0 = top limit, positive = down.
- `ZMAX` — the Z safety floor in steps (currently 6 feet of cable).
- `ZREF` — informational reference depth (3 feet) for default drops.
- `ZHOMED` — 1 if Z has been homed at least once this power cycle.
- `LX`, `LA`, `LY`, `LZTOP` — limit switch states. 1 = currently triggered.
- `SERVO` — current servo angle (25–90).
- `EN` — 1 if motor drivers are enabled.

### 3.5 The HOME sequence in detail

A single `HOME` command runs six phases. All can be cut short by `HALT`,
and any phase can fail with `STUCK` if its 20-second per-phase timeout
elapses without the relevant limit switch firing.

| Phase | FSM state           | Behavior |
|-------|---------------------|----------|
| 0     | `HOMING_Z_TO_LIMIT` | Drive Z upward until `Z_TOP_LIMIT` fires. Resync `Z=0`, set `ZHOMED=1`. |
| 1     | `HOMING_TO_LIMIT`   | Drive both X and A motors in `HOME_X_DIRECTION`. Each stops independently when its switch fires. |
| 2     | `HOMING_WAIT`       | 200 ms settle. |
| 3     | `HOMING_BACKOFF`    | Both X+A back off `HOME_BACKOFF_TURNS` (1 rev) in the opposite direction. |
| 4     | `HOMING_Y_TO_LIMIT` | Drive Y in `HOME_Y_DIRECTION` until `Y_LIMIT` fires. |
| 5     | `HOMING_Y_BACKOFF`  | Y backs off `HOME_Y_BACKOFF_TURNS` (¼ rev) in the opposite direction. |
| 6     | `HOMING_OPEN_CLAW`  | Servo sweeps to `SERVO_HOME_ANGLE` (open). |

On completion, the firmware emits `DONE <id> HOME OK` and returns to
`IDLE`. The claw is parked at the front-left corner of the machine
(per the `HOME_X_DIRECTION = -1` and `HOME_Y_DIRECTION = +1` constants).

### 3.6 Direction-flipping constants

These two constants in the firmware are the only place homing direction
is encoded. Flip the sign to reverse which side homes:

```cpp
const int HOME_X_DIRECTION = -1;   // currently homes X toward the front
const int HOME_Y_DIRECTION = +1;   // currently homes Y toward the left
```

Backoff directions are computed automatically as the negation of the
homing drive.

### 3.7 EVT messages

Mid-motion event lines. Useful for debugging:

| EVT                        | Meaning |
|----------------------------|---------|
| `EVT MOVING axis=X degrees=N steps=M`             | Move started. |
| `EVT LIMIT axis=X`                                | Limit switch fired during a regular move. |
| `EVT BACKUP axis=X steps=N`                       | Auto-backoff after limit hit. |
| `EVT Z_TOP_HIT resync_zero=1`                     | Z top switch fired; `Z` resynced to 0. |
| `EVT Z_CLAMPED requested_steps=N actual_steps=M reason=...` | Z target was clamped to a bound. |
| `EVT Z_RESYNC reason=halt z_pos=N`                | Z position recomputed after a HALT mid-Z-move. |
| `EVT SERVO_TARGET angle=N`                        | Servo target set. |
| `EVT SERVO_CLAMPED requested=N actual=M`          | Servo angle clamped to [25, 90]. |
| `EVT MOTORS ENABLED` / `EVT MOTORS DISABLED`      | Driver enable changed. |
| `EVT HOME_X_DRIVING` / `HOME_X_HIT` / `HOME_A_HIT` / `HOME_BOTH_HIT` / `HOME_X_TIMEOUT` / `HOME_A_TIMEOUT` | Homing progress (X/A phase). |
| `EVT HOME_BACKING_OFF`                            | Entering X/A backoff phase. |
| `EVT HOME_Y_DRIVING` / `HOME_Y_HIT` / `HOME_Y_NO_HIT` / `HOME_Y_BACKING_OFF` | Homing progress (Y phase). |
| `EVT HOME_OPENING_CLAW`                           | Entering claw-open phase. |
| `EVT HOME_STUCK axis=...`                         | Homing aborted due to phase timeout. |
| `EVT HOME_TIMEOUT`                                | Soft timeout: one X-side hit, the other didn't within 5 s. |

---

## 4. Hardware Pin Map

Arduino Uno + CNC Shield V3 + four A4988 drivers + 9g servo.

| Function                | Arduino pin | Shield label    |
|-------------------------|-------------|-----------------|
| X stepper STEP          | D2          | X.STEP          |
| X stepper DIR           | D5          | X.DIR           |
| Y stepper STEP          | D3          | Y.STEP          |
| Y stepper DIR           | D6          | Y.DIR           |
| Z stepper STEP          | D4          | Z.STEP          |
| Z stepper DIR           | D7          | Z.DIR           |
| A stepper STEP (X right gantry) | D12 | SpnEn (jumpered) |
| A stepper DIR  (X right gantry) | D13 | SpnDir (jumpered)|
| Driver Enable (all)     | D8          | EN              |
| X/A gantry left limit   | D9          | X+/X-           |
| X/A gantry right limit  | D10         | Y+/Y-           |
| Y limit (both ends OR'd)| D11         | Z+/Z-           |
| Z TOP limit             | A0          | Abort           |
| Claw servo signal       | A3          | (none — direct wire) |

All limit pins use `INPUT_PULLUP`. A switch is wired between the signal
pin and GND, so a closed switch reads LOW.

---

## 5. Function Inventory

### 5.1 `app.py` (FastAPI server + agent)

**Configuration constants**:
- `OPENAI_MODEL` — chat model used for the agent (default `gpt-4o-mini`).
- `X_DEGREES_PER_FOOT`, `Y_DEGREES_PER_FOOT`, `Z_DEGREES_PER_FOOT` —
  per-axis calibration, rendered into the system prompt.
- `DEFAULT_LOWER_DEGREES` — depth of `lower_claw()` with no argument
  (currently `Z_DEGREES_PER_FOOT * 3 = 1080`).

**Top-level objects**:
- `SYSTEM_PROMPT` — single rendered system prompt for every turn.
- `TOOLS` — list of OpenAI tool schemas. Static; no per-turn filtering.
- `controller` — singleton `ClawController` instance.
- `client` — singleton `OpenAI` client.

**Helper functions**:
- `_result_to_dict(result)` — serialize a `CommandResult` for tool output.
- `call_tool(name, args)` — dispatch table from tool name to controller method.

**HTTP endpoints**:
- `POST /turn` — main agent turn. Body: `{text, session_id}`. The pipeline
  described in §2.3.
- `POST /api/transcribe` — speech-to-text wrapper.
- `POST /api/tts` — text-to-speech wrapper (Cartesia by default).
- `GET  /api/state` and `GET /state` — return current `ClawState`.
- `POST /api/emergency-stop` and `/cmd/stop` — call `controller.halt()`.
- `POST /api/reset-emergency` and `/cmd/reset-emergency` — clear e-stop latch.
- `POST /cmd/move`, `/cmd/drop`, `/cmd/home` — legacy direct-command bridges,
  kept so older UI buttons keep working. Bypass the LLM.
- `GET /` — serves `index.html`.
- `GET /health` — liveness check.

### 5.2 `claw_controller.py`

**Models** (Pydantic):
- `ClawState` — cached machine state. Fields: `fsm_state`, `is_busy`,
  `x`, `y`, `z`, `limit_x`, `limit_a`, `limit_z`, `limit_z_top`,
  `servo_angle`, `motors_enabled`, `z_homed`, `emergency_stopped`,
  `last_error`, `last_command_at`.
- `CommandResult` — outcome of a single command: `ok`, `status`,
  `command`, `cmd_id`, `error`, `events`.
- `Limits` — soft bounds advertised to the agent (per-axis degree caps).

**Controller class** (`ClawController`):

Lifecycle:
- `start()` — open serial port (or sim), spawn reader thread, wait for
  the firmware's `READY` line, kick off the periodic `STATE?` poll.
- `close()` — stop reader, close port.

Send/receive plumbing (private):
- `_reader_loop()` — background thread; reads lines from serial.
- `_handle_line(line)` — classify firmware lines by leading token
  (`READY`/`INFO`/`ACK`/`DONE`/`ERR`/`EVT`/`STATE`/`PONG`).
- `_apply_state(tokens)` — parse a `STATE` line and update cached state.
- `_apply_event(tokens)` — apply a few `EVT` lines that mutate state.
- `_resolve_future(cmd_id, result)` — fulfill the awaiting future for a
  command id when its `DONE`/`ERR`/`PONG` arrives.
- `_send(command, timeout, expect_completion)` — write a command to
  the serial port and await its `DONE`. The base building block for
  every public method.
- `_sim_send(command)` — synchronous fake for `CLAW_SIM=1` mode.
- `_state_poll_loop()` — periodically issues `STATE?` to refresh the
  cache.

Public motion methods (each is `async` and returns `CommandResult`):
- `move_axis(axis, degrees)` — single-axis move. `axis ∈ {"x", "y", "z"}`.
- `lower_claw(degrees)` — descend by magnitude (always positive).
- `raise_claw()` — drive Z up to the top limit (sends `RAISE`).
- `home_z()` — same as `raise_claw` but explicitly named (sends `ZHOME`).
- `open_claw(angle=None)` — default 90.
- `close_claw(angle=None)` — default 25.
- `set_servo(angle)` — absolute servo angle.
- `home()` — full HOME sequence.
- `halt()` — emergency stop. Sets `emergency_stopped=True`.
- `reset_emergency()` — clear the e-stop latch.
- `ping()` — heartbeat.

Snapshot:
- `state()` — return a copy of the cached `ClawState`.

### 5.3 `claw_machine_V4_agent.ino`

**Configuration constants** (top of file):
- Pin defines, motor constants, servo angle limits.
- `STEPS_PER_REVOLUTION`, `STEPS_PER_DEGREE` — derived from microstep config.
- `MAX_SPEED`, `ACCELERATION` — XY motion profile.
- `Z_MAX_SPEED`, `Z_ACCELERATION` — Z motion profile (faster than XY).
- `Z_MIN_STEPS = 0`, `Z_MAX_STEPS` — software Z bounds.
- `Z_MAX_DOWN_DEGREES = 2160` (6 feet), `Z_REFERENCE_DOWN_DEGREES = 1080` (3 feet).
- `HOME_X_DIRECTION`, `HOME_Y_DIRECTION` — direction switches (§3.6).
- `HOME_PHASE_TIMEOUT_MS = 20000` — per-phase stuck-motor timeout.
- `LIMIT_DEBOUNCE_READS = 3` — consecutive LOW reads to count as a hit.

**State tracking globals**:
- `currentState` — the `enum State`.
- `activeCmdId`, `activeCmdLetter` — what command is currently running.
- `zPositionSteps`, `zPendingTarget`, `zMotorOrigin` — Z agent-frame model.
- `zPositionKnown` — true once Z top limit has fired this power cycle.
- `xMoveDirection`, `yMoveDirection`, `zMoveDirection` — set at the start
  of each move so backoff direction is known when a limit fires.
- `xHitLimit`, `aHitLimit`, `yHomeHit` — homing-phase progress flags.
- `xLimitDebounce`, `aLimitDebounce`, `yLimitDebounce`, `zTopLimitDebounce` —
  per-pin debounce counters.

**Core entry points**:
- `setup()` — pinModes, stepper config, servo attach, boot announcement.
  Reads Z top limit at boot; sets `zPositionKnown` if pressed.
- `loop()` — services serial polling, halt requests, servo updates, then
  dispatches into the FSM.

**Serial helpers**:
- `pollSerial()` — reads bytes from `Serial`, accumulates a line, calls
  `dispatchCommand()` on `\n`.
- `dispatchCommand()` — the giant `if/else` chain matching command keywords.
- `emitAck(cmd)`, `emitDone(cmd, status)`, `emitErr(id, reason)`,
  `emitStateLine(id)` — produce protocol-compliant response lines.

**Motion control (per FSM state)**:
- `runXAxis()`, `runXBackoff()` — X+A move with limit detection.
- `runYAxis()`, `runYBackoff()` — Y move with limit detection.
- `runZAxis()` — Z move with top-limit auto-resync on upward motion.
- `runHomingZToLimit()` — Phase 0 (Z up).
- `runHomingToLimit()`, `runHomingWait()`, `runHomingBackoff()` — Phases 1-3.
- `runHomingYToLimit()`, `runHomingYBackoff()` — Phases 4-5.
- `runHomingOpenClaw()` — Phase 6.

**Utilities**:
- `hardStop(stepper)` — zero out remaining travel immediately.
- `clampStopped(stepper)` — defensive per-tick "stay stopped" call.
- `debouncedLimitHit(pin, counter)` — debounced LOW read.
- `abortHomingStuck(axisLabel)` — stuck-motor abort path.
- `clampZTarget(steps)` — clamp a Z target to `[0, Z_MAX_STEPS]`.
- `currentZAgentPosition()` — convert motor-frame back to agent-frame.
- `enableMotors()`, `disableMotors()` — toggle the EN pin.
- `setServoTarget(angle)`, `updateServo()` — servo with rate-limited sweep.
- `serviceHalt()` — stop everything, emit `DONE ... HALTED`, return to IDLE.

---

## 6. Adding a New Tool: Worked Example

Suppose you want to add a `wiggle` tool that shakes the gantry to free a stuck
plushie. The change touches three files:

### Firmware (`claw_machine_V4_agent.ino`)
Add a `WIGGLE` command branch in `dispatchCommand`:
```cpp
if (strcmp(kw, "WIGGLE") == 0) {
  enableMotors();
  activeCmdLetter = 'W';
  emitAck("WIGGLE");
  // ... oscillate stepperX / stepperY a few cycles ...
  emitDone("WIGGLE", "OK");
  return;
}
```
No new FSM state is needed if the operation is short; for anything longer
than a few hundred ms, add a `WIGGLING` state and a `runWiggle()` tick
function so HALT can interrupt it.

### Controller (`claw_controller.py`)
Add a method on `ClawController`:
```python
async def wiggle(self) -> CommandResult:
    if self._state.emergency_stopped:
        return CommandResult(ok=False, status="ERR", command="WIGGLE",
                             cmd_id="-", error="Emergency stopped")
    self._state.last_command_at = time.monotonic()
    return await self._send("WIGGLE", timeout=10.0)
```

### Agent (`app.py`)
Append a tool schema to `TOOLS`:
```python
{
    "type": "function",
    "function": {
        "name": "wiggle",
        "description": "Shake the gantry briefly to free a stuck prize. "
                       "Use only when the user explicitly asks to 'wiggle' or "
                       "'jiggle'.",
        "parameters": {"type": "object", "properties": {}},
    },
},
```
And add a dispatch case in `call_tool`:
```python
if name == "wiggle":
    result = await controller.wiggle()
    return _result_to_dict(result)
```

That's the full pattern — one branch in three places.

---

## 7. Debugging Tips

- **Talk to the firmware directly**: open a serial terminal at 115200 and
  type commands by hand. Useful for confirming a switch is wired correctly
  before involving the LLM.
- **Watch state without LLM cost**: hit `GET /api/state` repeatedly, or
  open the browser UI which polls automatically.
- **Run without hardware**: set `CLAW_SIM=1` before launching the server.
  The controller fakes everything; the agent loop and tool dispatch all
  still execute.
- **See what the LLM saw**: every `/turn` response includes a
  `tool_events` array showing the exact tool calls and their results.
- **Step through homing**: send `HOME` at the serial terminal and watch
  the `EVT HOME_*` lines stream by. Each phase emits its own event, so
  you can pinpoint where it gets stuck.
- **Verify direction switches**: if homing drives the wrong way, the
  fastest fix is flipping `HOME_X_DIRECTION` or `HOME_Y_DIRECTION` in
  the firmware. Don't try to chase it through the agent layer.

---

## 8. Glossary

- **Agent frame** — the user-facing coordinate system. +Z is down, 0 is
  the top. Axis-positive directions are documented in the system prompt.
- **Motor frame** — raw `AccelStepper.currentPosition()` values. May
  differ in sign from the agent frame depending on motor wiring; the
  conversion is `Z_DOWN_SIGN` for Z, identity for X/Y.
- **STUCK** — a homing phase didn't complete within
  `HOME_PHASE_TIMEOUT_MS`. Surfaced to the agent so the LLM can tell
  the user "looks like a motor is stuck" and stop.
- **Z homed** — the firmware has seen the Z top limit fire at least
  once this power cycle. Until then, all Z values are best-effort
  guesses based on stepper-step counting.
