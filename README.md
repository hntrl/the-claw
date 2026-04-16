# The Claw

![the claw](.github/images/buzz-aliens.jpg)

## 1) Setup

1. Copy env file:
```bash
cp .env.example .env
```
2. Add `OPENAI_API_KEY` in `.env`.
3. Add Cartesia credentials (`CARTESIA_API_KEY`, `CARTESIA_VOICE_ID`) in `.env`.
4. Install dependencies:
```bash
uv sync
```

## 2) Run One Service

```bash
uv run uvicorn agent.app:app --host ${AGENT_HOST:-127.0.0.1} --port ${AGENT_PORT:-8000} --reload
```

Or use the one-command demo launcher:
```bash
./scripts/run_demo.sh
```

Health check:
```bash
curl http://127.0.0.1:8000/health
```

Open web console:
```bash
open http://127.0.0.1:8000/
```

## 3) Test the Agent

```bash
curl -X POST http://127.0.0.1:8000/turn \
  -H "Content-Type: application/json" \
  -d '{"text":"move left a little then drop","session_id":"demo-1"}'
```

CLI fallback:
```bash
uv run python scripts/demo_cli.py
```

## 4) Hands-Free Mic Script (VAD)

```bash
uv run python scripts/voice_demo.py
```

Flow:
1. Script continuously listens
2. Speech crosses `VAD_START_THRESHOLD` -> recording starts
3. Recording ends on trailing silence or `MAX_RECORD_SECONDS`
4. Transcription is sent to `/turn`
5. Reply is spoken with macOS `say`

Useful VAD tuning in `.env`:
- `VAD_START_THRESHOLD`: raise in noisy rooms
- `VAD_STOP_THRESHOLD`: raise to end turns faster
- `VAD_SILENCE_FRAMES_TO_STOP`: lower for snappier handoff
- `POST_TTS_COOLDOWN_SECONDS`: raise to avoid speaker retriggers

## 5) Web Demo

Features:
- Start/stop continuous listening with VAD
- Manual text fallback input
- Spoken agent replies from backend TTS (`/api/tts`, Cartesia by default)
- Live machine state panel
- Emergency stop / reset controls

Notes:
- Browser only captures microphone audio (`getUserMedia` + `MediaRecorder`).
- Transcription is performed by backend endpoint `POST /api/transcribe` using OpenAI.
- Reply synthesis is performed by backend endpoint `POST /api/tts` (Cartesia provider).
- This avoids dependency on browser cloud speech services like `webkitSpeechRecognition`.

## 6) API Endpoints (Single App)

- `POST /turn`
- `POST /api/transcribe`
- `POST /api/tts`
- `GET /api/state`
- `POST /api/emergency-stop`
- `POST /api/reset-emergency`
- `GET /state`
- `POST /cmd/move`
- `POST /cmd/drop`
- `POST /cmd/home`
- `POST /cmd/stop`
- `POST /cmd/reset-emergency`

Replace `ClawController` in `agent/app.py` with your real serial/PLC/GPIO hardware adapter.
