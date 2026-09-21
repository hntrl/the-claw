# Agent Runtime (Python)

This directory contains the realtime Python agent backend for the display demo.

## What it does

- Hosts a WebSocket server for the frontend on `ws://localhost:8787`.
- Runs OpenAI Agents SDK Realtime tool orchestration with local spoken responses.
- Delegates claw execution to local `common/claw_controller` backend module.
- Emits the display event contract consumed by `packages/web/src/hooks/useDisplaySocket.ts`.

## Install

From `packages/agent/`:

```bash
uv sync
```

If you prefer pip:

```bash
pip install -e .
```

## Run

From repo root:

```bash
cd packages/agent && uv run python server.py
```

Optional demo mode:

```bash
cd packages/agent && uv run python server.py --demo
```

Frontend URL:

```txt
http://localhost:5173/?ws=ws://localhost:8787
```

List local audio devices:

```bash
just agent-audio-devices
```

## Sending utterances

- Speak into your microphone when `--mic` is enabled.
- Or send websocket messages:

```json
{ "type": "utterance", "text": "grab the green capsule near the front left corner" }
```

Raw text over websocket:

```json
{ "type": "raw_text", "text": "grab the blue duck near front left" }
```

## Environment variables

- `AGENT_WS_HOST` default `0.0.0.0`
- `AGENT_WS_PORT` default `8787`
- `AGENT_RUNTIME` default `realtime` (only `realtime` is supported)
- `AGENT_SUCCESS_RATE` default `0.68`
- `AGENT_DEMO_INTERVAL_MS` default `14000`
- `CLAW_CONTROLLER_MODE` default `auto` (`auto`, `sim`, `serial`)
- `CLAW_SERIAL_PORT` default `auto`, which discovers a single USB serial device; set an explicit device path to override it
- `CLAW_SERIAL_VID` / `CLAW_SERIAL_PID` optional decimal or hexadecimal USB IDs used to select a device when more than one is attached
- `CLAW_SERIAL_MATCH` optional case-insensitive substring matched against the device metadata when more than one is attached
- `CLAW_SERIAL_BAUD` default `115200`
- `CLAW_SERIAL_TIMEOUT_S` default `1.0`
- `CLAW_MOVE_X_DEGREES` default `90`
- `CLAW_MOVE_Y_DEGREES` default `90`
- `CLAW_MOVE_Z_DOWN_DEGREES` default `360`
- `CLAW_MOVE_Z_UP_DEGREES` default `180`
- `CLAW_USE_RAISE_FOR_UP` default `1`
- `CLAW_RESULT_MODE` default `pending` (`pending` or `random`)
- `CLAW_RESULT_SUCCESS_RATE` default `0.68` (used when `CLAW_RESULT_MODE=random`)
- `OPENAI_API_KEY` required
- `OPENAI_PROJECT` optional
- `OPENAI_ORG` optional
- `OPENAI_REALTIME_MODEL` default `gpt-realtime-2.1`
- `OPENAI_REALTIME_VOICE` default `marin`
- `AGENT_REALTIME_PLAY_AUDIO` default `1`
- `AGENT_REALTIME_AUDIO_WRITE_TIMEOUT_S` default `2.0`, maximum time allowed for a local speaker write before playback is reset
- `AGENT_REALTIME_RECONNECT_INITIAL_S` default `0.25`, delay after the first failed reconnect attempt
- `AGENT_REALTIME_RECONNECT_MAX_S` default `5.0`, maximum exponential reconnect delay
- `AGENT_REALTIME_CONNECT_TIMEOUT_S` default `15.0`, maximum session connection/setup time
- `AGENT_REALTIME_CLOSE_TIMEOUT_S` default `3.0`, maximum failed-session cleanup time
- `AGENT_AUDIO_OUTPUT_DEVICE` optional (speaker device index or exact device name)
Notes:
- Arduino serial mode requires `pyserial` in the runtime environment.
- In `auto` mode, startup tries serial transport and falls back to simulation if unavailable.
- The WebSocket accepts text messages only; binary frames are ignored.

## Interrupt behavior

Submitting a newer nonempty text message interrupts the active model response and
closes local speaker playback immediately. It prevents unstarted hardware actions,
but does not claim to stop a command already sent to the physical controller; an
explicit urgent stop request is handled through the `halt` tool.

## Hardware-free reliability checks

Run the offline SDK/event replay and all Python tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

For an opt-in live API soak, with simulated claw hardware and silent audio paced
at playback speed:

```bash
.venv/bin/python scripts/realtime_soak.py --live --duration 210
```

The live test uses the configured API credential and incurs normal API usage.
It does not open microphones, speakers, or serial devices. See the
[reliability investigation](../../docs/realtime-v2-reliability.md) for reproduced
failures, recovery behavior, and validation limits.
