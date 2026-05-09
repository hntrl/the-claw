from __future__ import annotations

import asyncio
import contextlib
import io
import os
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd
from openai import OpenAI


def _pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _rms_energy(chunk: np.ndarray) -> float:
    if chunk.size == 0:
        return 0.0
    normalized = chunk.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(normalized * normalized)))


@dataclass
class MicConfig:
    sample_rate: int = int(os.getenv("MIC_SAMPLE_RATE", "16000"))
    max_record_seconds: int = int(os.getenv("MAX_RECORD_SECONDS", "6"))
    frame_ms: int = int(os.getenv("VAD_FRAME_MS", "30"))
    start_threshold: float = float(os.getenv("VAD_START_THRESHOLD", "0.020"))
    stop_threshold: float = float(os.getenv("VAD_STOP_THRESHOLD", "0.012"))
    min_speech_frames: int = int(os.getenv("VAD_MIN_SPEECH_FRAMES", "3"))
    silence_frames_to_stop: int = int(os.getenv("VAD_SILENCE_FRAMES_TO_STOP", "12"))
    preroll_frames: int = int(os.getenv("VAD_PREROLL_FRAMES", "8"))
    min_utterance_seconds: float = float(os.getenv("MIN_UTTERANCE_SECONDS", "0.5"))
    transcribe_model: str = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    input_device: str | int | None = os.getenv("AGENT_AUDIO_INPUT_DEVICE", "").strip() or None
    barge_in_enabled: bool = os.getenv("AGENT_ENABLE_BARGE_IN", "1").strip().lower() not in {"0", "false", "no"}
    barge_in_min_speech_frames: int = int(os.getenv("BARGE_IN_MIN_SPEECH_FRAMES", "0"))

    def __post_init__(self) -> None:
        if isinstance(self.input_device, str):
            with contextlib.suppress(ValueError):
                self.input_device = int(self.input_device)
        if self.barge_in_min_speech_frames <= 0:
            self.barge_in_min_speech_frames = self.min_speech_frames

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * (self.frame_ms / 1000.0))


def listen_for_utterance(
    config: MicConfig,
    *,
    on_speech_start: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bytes | None:
    pre_roll = deque(maxlen=config.preroll_frames)
    speech_frames: list[np.ndarray] = []
    speech_hits = 0
    barge_hits = 0
    silence_hits = 0
    in_speech = False
    barge_notified = False
    deadline = time.monotonic() + config.max_record_seconds

    with sd.InputStream(
        samplerate=config.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=config.frame_samples,
        device=config.input_device,
    ) as stream:
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return None

            chunk, _ = stream.read(config.frame_samples)
            mono = chunk[:, 0].copy()
            energy = _rms_energy(mono)
            pre_roll.append(mono)

            if not in_speech:
                if energy >= config.start_threshold:
                    speech_hits += 1
                    barge_hits += 1
                else:
                    speech_hits = max(0, speech_hits - 1)
                    barge_hits = max(0, barge_hits - 1)

                if (
                    on_speech_start
                    and config.barge_in_enabled
                    and not barge_notified
                    and barge_hits >= config.barge_in_min_speech_frames
                ):
                    barge_notified = True
                    on_speech_start()

                if speech_hits >= config.min_speech_frames:
                    in_speech = True
                    speech_frames.extend(list(pre_roll))
                    if on_speech_start and config.barge_in_enabled and not barge_notified:
                        barge_notified = True
                        on_speech_start()
            else:
                speech_frames.append(mono)
                if energy < config.stop_threshold:
                    silence_hits += 1
                else:
                    silence_hits = 0

                if silence_hits >= config.silence_frames_to_stop:
                    break

    if not speech_frames:
        return None

    pcm = np.concatenate(speech_frames).astype(np.int16)
    if len(pcm) / config.sample_rate < config.min_utterance_seconds:
        return None

    return _pcm_to_wav_bytes(pcm, config.sample_rate)


class OpenAITranscriber:
    def __init__(self, model: str) -> None:
        self._client = OpenAI()
        self._model = model

    async def transcribe(self, wav_bytes: bytes) -> str:
        def _transcribe_sync() -> str:
            result = self._client.audio.transcriptions.create(
                model=self._model,
                file=("mic.wav", wav_bytes, "audio/wav"),
            )
            return (result.text or "").strip()

        return await asyncio.to_thread(_transcribe_sync)
