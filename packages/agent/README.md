# Agent Runtime (Python)

This directory contains Python agent runtimes for the display demo.

## What it does

- Hosts a WebSocket server for the frontend on `ws://localhost:8787`.
- Supports:
  - `pipecat` runtime (existing pipeline)
  - `realtime` runtime (OpenAI Realtime API, for side-by-side comparison)
- Delegates claw execution to local `common/claw_controller` backend module (noop or legacy Arduino protocol), based on `packages/old/agent/claw_controller.py`.
- Uses one shared local speaker sink implementation for playback across both runtimes.
- Emits the existing display event contract consumed by `packages/web/src/hooks/useDisplaySocket.ts`.

## Pipeline stages

1. `VoiceStartProcessor`
2. `SpeechToTextProcessor`
3. `AgentProcessor`
4. `CartesiaMarkupProcessor`
5. `TTSSpeakProcessor`
6. `DisplayEventDispatchProcessor`

`AgentProcessor` runs the LLM/tool loop through LangChain `create_agent` (using `langchain-openai`) in this order:

1. `parse_intent`
2. `select_target`
3. `plan_motion`
4. `execute_claw`

After LLM response text is generated, `CartesiaMarkupProcessor` extracts prefix tags like
`<emotion value="excited"/><speed ratio="1.05"/>` and emits separate websocket style events:

- `{ "type": "emotion", "mood": "...", "emotion": "...", "speed": 1.05 }`
- `{ "type": "emotion_clear" }` after speech playback

Display updates are frame-driven: processors emit `DisplayEventFrame` into the pipeline,
and `DisplayEventDispatchProcessor` is the only stage that sends websocket JSON.

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

Choose runtime explicitly:

```bash
cd packages/agent && uv run python server.py --runtime pipecat
cd packages/agent && uv run python server.py --runtime realtime
```

Optional demo mode:

```bash
cd packages/agent && uv run python server.py --demo
```

Use real microphone input:

```bash
cd packages/agent && uv run python server.py --mic
```

Frontend URL:

```txt
http://localhost:5173/?ws=ws://localhost:8787
```

List local audio output devices:

```bash
just agent-audio-devices
```

## Sending utterances

- Type into the agent terminal directly.
- Prefix terminal input with `/text ` to bypass STT:

```txt
/text grab the blue duck near front left
```

- Speak into your microphone when `--mic` is enabled.
- Or send websocket messages:

```json
{ "type": "utterance", "text": "grab the green capsule near the front left corner" }
```

Raw text over websocket (bypass STT):

```json
{ "type": "raw_text", "text": "grab the blue duck near front left" }
```

## Environment variables

- `AGENT_WS_HOST` default `0.0.0.0`
- `AGENT_WS_PORT` default `8787`
- `AGENT_RUNTIME` default `pipecat` (`pipecat` or `realtime`)
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
- `OPENAI_API_KEY` required (LLM tools + `--mic`)
- `OPENAI_PROJECT` optional (pin OpenAI project for API requests)
- `OPENAI_ORG` optional (pin OpenAI organization for API requests)
- `OPENAI_MODEL` default `gpt-4.1-mini`
- `OPENAI_TRANSCRIBE_MODEL` default `gpt-4o-mini-transcribe` (Pipecat `--mic` path only)
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
- `AGENT_TTS_ENABLED` default `1`
- `CARTESIA_API_KEY` required for Pipecat local TTS
- `CARTESIA_MODEL_ID` default `sonic-3`
- `CARTESIA_VOICE_ID` default `f786b574-daa5-4673-aa0c-cbe3e8534c02`
- `CARTESIA_LANGUAGE` default `en`
- `CARTESIA_SAMPLE_RATE` default `44100`
- `MIC_SAMPLE_RATE` default `16000` for Pipecat, minimum `24000` for realtime `--mic`
- `MAX_RECORD_SECONDS` default `6`
- `VAD_FRAME_MS` default `30`
- `VAD_START_THRESHOLD` default `0.020`
- `VAD_STOP_THRESHOLD` default `0.012`
- `VAD_MIN_SPEECH_FRAMES` default `3`
- `VAD_SILENCE_FRAMES_TO_STOP` default `12`
- `VAD_PREROLL_FRAMES` default `8`
- `MIN_UTTERANCE_SECONDS` default `0.5`
- `AGENT_ENABLE_BARGE_IN` default `1`
- `BARGE_IN_MIN_SPEECH_FRAMES` default `5` (use higher value to reduce false barge-in from speaker bleed)

Notes:
- Arduino serial mode requires `pyserial` in the runtime environment.
- In `auto` mode, startup tries serial transport and falls back to simulation if unavailable.

Mic input behavior by runtime:
- `pipecat`: local capture -> OpenAI transcription (`OPENAI_TRANSCRIBE_MODEL`) -> text turn
- `realtime`: local capture -> direct audio input to Realtime model (no separate transcription call); uses `MIC_SAMPLE_RATE >= 24000`

## Interrupt behavior

- The service detects speech onset during active execution.
- On barge-in, it requests interruption and aborts the in-flight execution stage.
- Active TTS speech is also terminated on interrupt.
- The new utterance is then processed from the queue.

For `realtime` runtime, barge-in is disabled by default to prevent speaker talkback loops. Enable it with `OPENAI_REALTIME_BARGE_IN_ENABLED=1` (prefer headset/echo-cancelled input), and interruption sends `response.cancel` with best-effort truncation to the Realtime session.
