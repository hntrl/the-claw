from __future__ import annotations

from collections import deque
import io
import os
import subprocess
import time
import wave

import httpx
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_MODEL_TRANSCRIBE = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000")
SAMPLE_RATE = int(os.getenv("MIC_SAMPLE_RATE", "16000"))
MAX_RECORD_SECONDS = int(os.getenv("MAX_RECORD_SECONDS", "6"))
VAD_FRAME_MS = int(os.getenv("VAD_FRAME_MS", "30"))
VAD_START_THRESHOLD = float(os.getenv("VAD_START_THRESHOLD", "0.020"))
VAD_STOP_THRESHOLD = float(os.getenv("VAD_STOP_THRESHOLD", "0.012"))
VAD_MIN_SPEECH_FRAMES = int(os.getenv("VAD_MIN_SPEECH_FRAMES", "3"))
VAD_SILENCE_FRAMES_TO_STOP = int(os.getenv("VAD_SILENCE_FRAMES_TO_STOP", "12"))
VAD_PREROLL_FRAMES = int(os.getenv("VAD_PREROLL_FRAMES", "8"))
MIN_UTTERANCE_SECONDS = float(os.getenv("MIN_UTTERANCE_SECONDS", "0.5"))
POST_TTS_COOLDOWN_SECONDS = float(os.getenv("POST_TTS_COOLDOWN_SECONDS", "0.8"))

FRAME_SAMPLES = int(SAMPLE_RATE * (VAD_FRAME_MS / 1000.0))


def pcm_to_wav_bytes(pcm: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def rms_energy(chunk: np.ndarray) -> float:
    if chunk.size == 0:
        return 0.0
    normalized = chunk.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(normalized * normalized)))


def listen_for_utterance() -> bytes | None:
    print("Listening... speak naturally.")
    pre_roll = deque(maxlen=VAD_PREROLL_FRAMES)
    speech_frames: list[np.ndarray] = []
    speech_hits = 0
    in_speech = False
    silence_hits = 0
    deadline = time.monotonic() + MAX_RECORD_SECONDS

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
    ) as stream:
        while time.monotonic() < deadline:
            chunk, _ = stream.read(FRAME_SAMPLES)
            mono = chunk[:, 0].copy()
            energy = rms_energy(mono)
            pre_roll.append(mono)

            if not in_speech:
                if energy >= VAD_START_THRESHOLD:
                    speech_hits += 1
                else:
                    speech_hits = max(0, speech_hits - 1)

                if speech_hits >= VAD_MIN_SPEECH_FRAMES:
                    in_speech = True
                    speech_frames.extend(list(pre_roll))
                    print("Speech detected.")
            else:
                speech_frames.append(mono)
                if energy < VAD_STOP_THRESHOLD:
                    silence_hits += 1
                else:
                    silence_hits = 0
                if silence_hits >= VAD_SILENCE_FRAMES_TO_STOP:
                    break

    if not speech_frames:
        return None

    pcm = np.concatenate(speech_frames).astype(np.int16)
    duration_s = len(pcm) / SAMPLE_RATE
    if duration_s < MIN_UTTERANCE_SECONDS:
        return None
    return pcm_to_wav_bytes(pcm)


def transcribe(client: OpenAI, wav_bytes: bytes) -> str:
    transcription = client.audio.transcriptions.create(
        model=OPENAI_MODEL_TRANSCRIBE,
        file=("mic.wav", wav_bytes, "audio/wav"),
    )
    return (transcription.text or "").strip()


def speak(text: str) -> None:
    subprocess.run(["say", text], check=False)


def main() -> None:
    print("Voice Claw Demo")
    print("Hands-free mode: automatic speech detection enabled.")
    print("Press Ctrl+C to quit.")

    client = OpenAI()
    session_id = "voice-session"

    try:
        while True:
            wav_bytes = listen_for_utterance()
            if not wav_bytes:
                continue

            text = transcribe(client, wav_bytes)
            if not text:
                print("Could not transcribe speech, listening again.")
                continue

            print(f"You said: {text}")
            resp = httpx.post(
                f"{AGENT_BASE_URL}/turn",
                json={"text": text, "session_id": session_id},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["reply"]
            print(f"Agent: {reply}")
            speak(reply)

            tool_events = data.get("tool_events", [])
            if tool_events:
                print("Tools:")
                for ev in tool_events:
                    print(f"- {ev['tool']} {ev['args']}")

            time.sleep(POST_TTS_COOLDOWN_SECONDS)
    except KeyboardInterrupt:
        print("\nExiting voice demo.")


if __name__ == "__main__":
    main()

