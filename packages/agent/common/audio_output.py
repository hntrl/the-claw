from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Literal

import sounddevice as sd

logger = logging.getLogger(__name__)

AudioDType = Literal["int16", "float32"]


class LocalAudioOutput:
    """Shared local speaker sink used by the realtime runtime."""

    def __init__(self) -> None:
        output_device = os.getenv("AGENT_AUDIO_OUTPUT_DEVICE", "").strip()
        self._configured_device: str | int | None = self._parse_output_device(output_device)
        self._configured_device_invalid = False
        self._stream: sd.RawOutputStream | None = None
        self._stream_sample_rate: int | None = None
        self._stream_dtype: AudioDType | None = None
        self._stream_channels: int | None = None
        self._lock = asyncio.Lock()

    async def write(
        self,
        audio: bytes,
        *,
        sample_rate: int,
        dtype: AudioDType,
        channels: int = 1,
    ) -> None:
        if not audio:
            return
        async with self._lock:
            await self._ensure_stream(sample_rate=sample_rate, dtype=dtype, channels=channels)
            if self._stream is None:
                return
            await asyncio.to_thread(self._stream.write, audio)

    async def close(self) -> None:
        async with self._lock:
            await self._close_stream_locked()

    def _parse_output_device(self, value: str) -> str | int | None:
        if not value:
            return None
        with contextlib.suppress(ValueError):
            return int(value)
        return value

    def _select_fallback_output_device(self) -> str | int | None:
        with contextlib.suppress(Exception):
            devices = sd.query_devices()
            if isinstance(devices, tuple):
                devices = list(devices)
            if isinstance(devices, list):
                for idx, device in enumerate(devices):
                    if isinstance(device, dict) and int(device.get("max_output_channels") or 0) > 0:
                        logger.warning(
                            "No default output device set; using fallback device %s (%s)",
                            idx,
                            device.get("name") or "unknown",
                        )
                        return idx
        return None

    def _resolve_output_device(self) -> str | int | None:
        if self._configured_device is not None and not self._configured_device_invalid:
            with contextlib.suppress(Exception):
                info = sd.query_devices(self._configured_device, "output")
                if isinstance(info, dict) and int(info.get("max_output_channels") or 0) > 0:
                    return self._configured_device
            logger.warning(
                "Configured AGENT_AUDIO_OUTPUT_DEVICE=%r is invalid/unavailable; falling back to default output device",
                self._configured_device,
            )
            self._configured_device_invalid = True

        default_devices = sd.default.device
        output_idx: int | None = None
        if isinstance(default_devices, (list, tuple)) and len(default_devices) >= 2:
            output_candidate = default_devices[1]
            if isinstance(output_candidate, int) and output_candidate >= 0:
                output_idx = output_candidate
        elif isinstance(default_devices, int) and default_devices >= 0:
            output_idx = default_devices

        if output_idx is not None:
            return output_idx
        return self._select_fallback_output_device()

    async def _ensure_stream(self, *, sample_rate: int, dtype: AudioDType, channels: int) -> None:
        if (
            self._stream is not None
            and self._stream_sample_rate == sample_rate
            and self._stream_dtype == dtype
            and self._stream_channels == channels
        ):
            return

        await self._close_stream_locked()

        resolved_output_device = self._resolve_output_device()
        if resolved_output_device is None and self._configured_device is None:
            logger.warning("No audio output device available; speaker output is disabled")
            return

        stream_kwargs = {
            "samplerate": sample_rate,
            "channels": channels,
            "dtype": dtype,
        }
        if resolved_output_device is not None:
            stream_kwargs["device"] = resolved_output_device

        try:
            stream = sd.RawOutputStream(**stream_kwargs)
            stream.start()
        except Exception as exc:
            # Device selection can drift across restarts/sleep. Retry once without explicit device.
            if resolved_output_device is not None:
                logger.warning(
                    "Audio output device %r failed (%s); retrying with system default",
                    resolved_output_device,
                    exc,
                )
                fallback_kwargs = {
                    "samplerate": sample_rate,
                    "channels": channels,
                    "dtype": dtype,
                }
                stream = sd.RawOutputStream(**fallback_kwargs)
                stream.start()
                resolved_output_device = None
            else:
                raise
        self._stream = stream
        self._stream_sample_rate = sample_rate
        self._stream_dtype = dtype
        self._stream_channels = channels
        logger.info(
            "Audio playback started (rate=%s, dtype=%s, device=%s)",
            sample_rate,
            dtype,
            resolved_output_device if resolved_output_device is not None else "default",
        )

    async def _close_stream_locked(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        self._stream_sample_rate = None
        self._stream_dtype = None
        self._stream_channels = None
        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()
