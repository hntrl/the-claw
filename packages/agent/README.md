# Agent Runtime (Python + Pipecat)

This directory contains a real Python Pipecat service for the display demo.

## What it does

- Hosts a WebSocket server for the frontend on `ws://localhost:8787`.
- Runs utterances through a Pipecat `Pipeline` with custom `FrameProcessor` stages.
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

Optional demo mode:

```bash
cd packages/agent && uv run python server.py --demo
```

Use real microphone input + STT:

```bash
cd packages/agent && uv run python server.py --mic
```

Frontend URL:

```txt
http://localhost:5173/?ws=ws://localhost:8787
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
- `AGENT_SUCCESS_RATE` default `0.68`
- `AGENT_DEMO_INTERVAL_MS` default `14000`
- `OPENAI_API_KEY` required (LLM tools + `--mic`)
- `OPENAI_MODEL` default `gpt-4.1-mini`
- `OPENAI_TRANSCRIBE_MODEL` default `gpt-4o-mini-transcribe`
- `AGENT_TTS_ENABLED` default `1`
- `CARTESIA_API_KEY` required when TTS is enabled
- `CARTESIA_MODEL_ID` default `sonic-3`
- `CARTESIA_VOICE_ID` default `f786b574-daa5-4673-aa0c-cbe3e8534c02`
- `CARTESIA_LANGUAGE` default `en`
- `CARTESIA_SAMPLE_RATE` default `44100`
- `MIC_SAMPLE_RATE` default `16000`
- `MAX_RECORD_SECONDS` default `6`
- `VAD_FRAME_MS` default `30`
- `VAD_START_THRESHOLD` default `0.020`
- `VAD_STOP_THRESHOLD` default `0.012`
- `VAD_MIN_SPEECH_FRAMES` default `3`
- `VAD_SILENCE_FRAMES_TO_STOP` default `12`
- `VAD_PREROLL_FRAMES` default `8`
- `MIN_UTTERANCE_SECONDS` default `0.5`

## Interrupt behavior

- The service detects speech onset during active execution.
- On barge-in, it requests interruption and aborts the in-flight execution stage.
- Active TTS speech is also terminated on interrupt.
- The new utterance is then processed from the queue.
