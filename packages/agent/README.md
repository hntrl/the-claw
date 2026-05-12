# Agent Runtime (Python)

This directory contains the realtime Python agent backend for the display demo.

## What it does

- Hosts a WebSocket server for the frontend on `ws://localhost:8787`.
- Runs OpenAI Realtime voice + tool orchestration.
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

Use real microphone input:

```bash
cd packages/agent && uv run python server.py --mic
```

By default, `--mic` is push-to-talk in the Python process: hold `Right Option`
(`AGENT_MIC_PTT_KEY=alt_r`) to transmit.

Frontend URL:

```txt
http://localhost:5173/?ws=ws://localhost:8787
```

List local audio devices:

```bash
just agent-audio-devices
```

## Sending utterances

- Type into the agent terminal directly.
- Prefix terminal input with `/text ` to bypass speech input:

```txt
/text grab the blue duck near front left
```

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
- `CLAW_SERIAL_PORT` default `/dev/tty.usbmodem`
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
- `OPENAI_REALTIME_MODEL` default `gpt-realtime-2`
- `OPENAI_REALTIME_VOICE` default `marin`
- `OPENAI_REALTIME_TURN_EAGERNESS` default `low`
- `OPENAI_REALTIME_TURN_CREATE_RESPONSE` default `1`
- `OPENAI_REALTIME_TURN_INTERRUPT_RESPONSE` default `0`
- `OPENAI_REALTIME_BARGE_IN_ENABLED` default `0` (recommended on speakers; set `1` on headset)
- `OPENAI_REALTIME_BARGE_IN_GRACE_MS` default `900`
- `OPENAI_REALTIME_NOISE_REDUCTION` default `near_field` (`near_field` or `far_field`)
- `OPENAI_REALTIME_SPEED` optional speaking rate for realtime voice output (recommended `1.1` to `1.4`)
- `OPENAI_REALTIME_MIN_INPUT_AUDIO_MS` default `120`, minimum PCM depth batched before forwarding to Realtime
- `AGENT_REALTIME_PLAY_AUDIO` default `1`
- `AGENT_AUDIO_OUTPUT_DEVICE` optional (speaker device index or exact device name)
- `AGENT_AUDIO_INPUT_DEVICE` optional (microphone device index or exact device name)
- `AGENT_MIC_PTT_ENABLED` default `1` (`--mic` keyboard push-to-talk gate in Python process)
- `AGENT_MIC_PTT_KEY` default `alt_r` (uses `pynput.keyboard.Key` names, e.g. `alt_r`)
- `MIC_SAMPLE_RATE` default `24000` (`--mic` realtime input)
- `VAD_FRAME_MS` default `30` (`--mic` stream block size)

Notes:
- Arduino serial mode requires `pyserial` in the runtime environment.
- In `auto` mode, startup tries serial transport and falls back to simulation if unavailable.
- With `--mic`, incoming websocket binary audio is ignored so mic source stays the computer input device.

`--mic` keyboard capture depends on `pynput` and may require accessibility/input
monitoring permission from the OS.

## Interrupt behavior

- The service detects speech onset during active execution.
- On barge-in, it requests interruption and aborts the in-flight execution stage.
- Active TTS speech is also terminated on interrupt.
- The new utterance is then processed from the queue.

Barge-in is disabled by default to prevent speaker talkback loops. Enable it with
`OPENAI_REALTIME_BARGE_IN_ENABLED=1` (prefer headset/echo-cancelled input), and
interruption sends `response.cancel` with best-effort truncation to the Realtime
session.
