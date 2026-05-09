from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import signal
import sys
import wave
from collections.abc import Sequence
from contextlib import suppress
from typing import Protocol

import websockets
from websockets.exceptions import ConnectionClosed
from dotenv import load_dotenv

from common.broadcaster import DisplayBroadcaster

DEMO_PHRASES: tuple[str, ...] = (
    "grab the green capsule near the front left corner",
    "move right then forward and drop on the blue duck",
    "pick up the red ball near the middle",
)


class AgentRuntime(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit_utterance(self, text: str, *, source: str = "stdin") -> None: ...
    async def submit_raw_text(self, text: str, *, source: str = "text") -> None: ...
    async def on_speech_started(self, source: str = "mic") -> None: ...


def _wav_to_pcm16_mono(wav_bytes: bytes) -> bytes | None:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
    except Exception:
        return None

    if sample_width != 2:
        return None
    if channels == 1:
        return frames
    # Keep first channel from interleaved PCM16.
    if channels > 1:
        out = bytearray()
        frame_width = channels * 2
        for i in range(0, len(frames), frame_width):
            out.extend(frames[i : i + 2])
        return bytes(out)
    return None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


async def _stdin_loop(agent: AgentRuntime, stop_event: asyncio.Event) -> None:
    if not sys.stdin.isatty():
        return

    print("utterance> ", end="", flush=True)
    while not stop_event.is_set():
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.05)
            continue

        text = line.strip()
        if not text:
            print("utterance> ", end="", flush=True)
            continue
        if text == "/quit":
            stop_event.set()
            break
        if text == "/help":
            print("commands: /help, /quit, /text <prompt>")
            print("utterance> ", end="", flush=True)
            continue

        if text.startswith("/text "):
            await agent.submit_raw_text(text.removeprefix("/text "), source="stdin-text")
        else:
            await agent.submit_utterance(text, source="stdin")
        print("utterance> ", end="", flush=True)


async def _demo_loop(agent: AgentRuntime, stop_event: asyncio.Event, interval_ms: int) -> None:
    index = 0
    while not stop_event.is_set():
        phrase = DEMO_PHRASES[index % len(DEMO_PHRASES)]
        index += 1
        await agent.submit_utterance(phrase, source="demo")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_ms / 1000)
        except asyncio.TimeoutError:
            continue


async def _mic_loop(agent: AgentRuntime, stop_event: asyncio.Event) -> None:
    from pipecat_service.microphone import MicConfig, OpenAITranscriber, listen_for_utterance

    config = MicConfig()
    loop = asyncio.get_running_loop()
    selected_input = config.input_device if config.input_device is not None else "default"

    if hasattr(agent, "submit_audio_input"):
        import queue
        import sounddevice as sd

        if config.sample_rate < 24000:
            print(
                f"[agent] MIC_SAMPLE_RATE={config.sample_rate} is below realtime minimum; "
                "using 24000 for direct realtime audio input"
            )
            config.sample_rate = 24000
        print(
            f"[agent] microphone mode enabled (streaming realtime audio input, input={selected_input})"
        )

        audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=256)

        def _on_audio(indata, _frames, _time_info, status) -> None:
            if status:
                return
            mono = indata[:, 0].copy()
            chunk = mono.tobytes()
            try:
                audio_queue.put_nowait(chunk)
            except queue.Full:
                # Drop oldest-ish data under backpressure to keep stream realtime.
                pass

        try:
            with sd.InputStream(
                samplerate=config.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=config.frame_samples,
                device=config.input_device,
                callback=_on_audio,
            ):
                submit_audio_input = getattr(agent, "submit_audio_input")
                while not stop_event.is_set():
                    try:
                        chunk = await asyncio.to_thread(audio_queue.get, True, 0.2)
                    except queue.Empty:
                        continue
                    if not chunk:
                        continue
                    await submit_audio_input(chunk, source="mic")
        except Exception as exc:
            print(f"[agent] realtime microphone stream failed: {exc}")
        return

    transcriber = OpenAITranscriber(config.transcribe_model)
    print(f"[agent] microphone mode enabled (input={selected_input})")
    while not stop_event.is_set():
        def _on_speech_start() -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(agent.on_speech_started(source="mic"))
            )

        wav_bytes = await asyncio.to_thread(
            listen_for_utterance,
            config,
            on_speech_start=_on_speech_start,
            should_stop=stop_event.is_set,
        )
        if not wav_bytes:
            continue

        try:
            text = await transcriber.transcribe(wav_bytes)
        except Exception as exc:
            print(f"[agent] transcription failed: {exc}")
            continue

        if not text:
            continue

        print(f"[mic] {text}")
        await agent.submit_utterance(text, source="mic")


def _parse_client_input(message: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        normalized = message.strip()
        return ("utterance", normalized) if normalized else None

    if not isinstance(payload, dict):
        return None

    text = payload.get("text")
    if not isinstance(text, str):
        return None
    normalized = text.strip()
    if not normalized:
        return None

    payload_type = payload.get("type")
    if payload_type == "raw_text":
        return ("raw_text", normalized)
    if payload_type == "utterance":
        return ("utterance", normalized)
    return None


async def run(args: argparse.Namespace) -> None:
    load_dotenv(override=False)

    host = os.getenv("AGENT_WS_HOST", "0.0.0.0")
    port = int(os.getenv("AGENT_WS_PORT", os.getenv("PORT", "8787")))
    success_rate = float(os.getenv("AGENT_SUCCESS_RATE", "0.68"))
    demo_interval_ms = int(os.getenv("AGENT_DEMO_INTERVAL_MS", "14000"))
    runtime = args.runtime or os.getenv("AGENT_RUNTIME", "pipecat")
    runtime = runtime.strip().lower()

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required (LLM tool-calling + optional mic STT)")

    broadcaster = DisplayBroadcaster()
    if runtime == "realtime":
        from realtime_service.service import RealtimeClawVoiceService

        agent: AgentRuntime = RealtimeClawVoiceService(broadcaster, success_rate=success_rate)
    elif runtime == "pipecat":
        from pipecat_service.service import PipecatClawVoiceService

        agent = PipecatClawVoiceService(broadcaster, success_rate=success_rate)
    else:
        raise RuntimeError("Unsupported runtime. Expected one of: pipecat, realtime")
    stop_event = asyncio.Event()

    try:
        await agent.start()
    except Exception as exc:
        if runtime == "realtime" and "invalid_model" in str(exc).lower():
            raise RuntimeError(
                "Realtime startup failed with invalid_model for "
                f"{os.getenv('OPENAI_REALTIME_MODEL', 'gpt-realtime-2')}. "
                "Verify OPENAI_API_KEY/OPENAI_PROJECT point to the project with Realtime 2 access."
            ) from exc
        raise

    async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
        await broadcaster.add_client(websocket)
        with suppress(ConnectionClosed, OSError):
            await websocket.send(json.dumps({"type": "state", "state": "attract"}))

        warned_binary_unsupported = False
        try:
            async for message in websocket:
                if isinstance(message, (bytes, bytearray, memoryview)):
                    submit_audio_input = getattr(agent, "submit_audio_input", None)
                    if callable(submit_audio_input):
                        await submit_audio_input(bytes(message), source="ws-mic")
                    elif not warned_binary_unsupported:
                        warned_binary_unsupported = True
                        print(
                            "[agent] received websocket binary audio, but this runtime "
                            "does not support direct audio input; ignoring chunks"
                        )
                    continue
                if not isinstance(message, str):
                    continue
                parsed = _parse_client_input(message)
                if not parsed:
                    continue
                input_type, text = parsed
                if input_type == "raw_text":
                    await agent.submit_raw_text(text, source="ws-text")
                else:
                    await agent.submit_utterance(text, source="ws")
        except (ConnectionClosed, OSError):
            pass
        finally:
            await broadcaster.remove_client(websocket)

    ws_server = await websockets.serve(handle_client, host, port)

    print(f"[agent] websocket listening on ws://{host}:{port}")
    print(f"[agent] runtime: {runtime}")
    print(f"[agent] success rate: {success_rate}")
    if args.demo:
        print(f"[agent] demo mode enabled ({demo_interval_ms}ms interval)")
    if args.mic:
        if runtime == "realtime":
            print(
                "[agent] --mic streams audio directly to realtime model "
                "(OpenAI semantic turn detection enabled)"
            )
            print(
                "[agent] realtime mic gate: "
                f"ducking={'on' if _env_bool('OPENAI_REALTIME_INPUT_DUCKING', True) else 'off'}, "
                f"barge={'on' if _env_bool('OPENAI_REALTIME_BARGE_IN_ENABLED', False) else 'off'}, "
                f"barge_min_rms={os.getenv('OPENAI_REALTIME_BARGE_IN_MIN_RMS', '0.03')}, "
                f"barge_min_ms={os.getenv('OPENAI_REALTIME_BARGE_IN_MIN_MS', '120')}"
            )
        else:
            print(f"[agent] using model {os.getenv('OPENAI_TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe')} for mic STT")
    if runtime == "realtime":
        print(f"[agent] realtime model: {os.getenv('OPENAI_REALTIME_MODEL', 'gpt-realtime-2')}")
    if sys.stdin.isatty():
        print("[agent] type an utterance and press enter (/help, /quit)")

    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_stdin_loop(agent, stop_event)),
    ]
    if args.demo:
        tasks.append(asyncio.create_task(_demo_loop(agent, stop_event, demo_interval_ms)))
    if args.mic:
        tasks.append(asyncio.create_task(_mic_loop(agent, stop_event)))

    await stop_event.wait()

    ws_server.close()
    await ws_server.wait_closed()

    for task in tasks:
        task.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.gather(*tasks)

    await agent.stop()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pipecat-powered claw display agent")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Automatically enqueue demo utterances in a loop.",
    )
    parser.add_argument(
        "--mic",
        action="store_true",
        help="Capture real microphone utterances (runtime-specific handling).",
    )
    parser.add_argument(
        "--runtime",
        choices=("pipecat", "realtime"),
        default=None,
        help="Choose runtime backend (default: AGENT_RUNTIME env var or pipecat).",
    )
    return parser.parse_args(argv)


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
