from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class MicConfig:
    sample_rate: int = int(os.getenv("MIC_SAMPLE_RATE", "24000"))
    frame_ms: int = int(os.getenv("VAD_FRAME_MS", "30"))
    input_device: str | int | None = os.getenv("AGENT_AUDIO_INPUT_DEVICE", "").strip() or None

    def __post_init__(self) -> None:
        if isinstance(self.input_device, str):
            with suppress(ValueError):
                self.input_device = int(self.input_device)

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * (self.frame_ms / 1000.0))


def _normalize_key_name(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


class PushToTalkGate:
    def __init__(self, *, enabled: bool, key_name: str) -> None:
        self.enabled = enabled
        self.key_name = key_name
        self._pressed = threading.Event()
        self._listener: object | None = None
        self._target_key: object | None = None

    def start(self) -> None:
        if not self.enabled:
            return

        try:
            from pynput import keyboard
        except Exception as exc:
            self.enabled = False
            print(
                "[agent] mic push-to-talk disabled: missing keyboard hook backend "
                f"({exc}). Install dependencies with `cd packages/agent && uv sync`."
            )
            return

        aliases = {
            "altright": "alt_r",
            "altr": "alt_r",
            "rightalt": "alt_r",
            "rightoption": "alt_r",
            "optionright": "alt_r",
            "roption": "alt_r",
        }
        normalized = _normalize_key_name(self.key_name)
        resolved = aliases.get(normalized, normalized)
        target_key = getattr(keyboard.Key, resolved, None)
        if target_key is None:
            if len(self.key_name) == 1:
                target_key = keyboard.KeyCode.from_char(self.key_name)
            else:
                self.enabled = False
                print(
                    f"[agent] mic push-to-talk disabled: unsupported key '{self.key_name}'."
                )
                return

        self._target_key = target_key

        def _on_press(key: object) -> None:
            if key == self._target_key:
                self._pressed.set()

        def _on_release(key: object) -> None:
            if key == self._target_key:
                self._pressed.clear()

        listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
        listener.daemon = True
        listener.start()
        self._listener = listener

    def stop(self) -> None:
        self._pressed.clear()
        listener = self._listener
        self._listener = None
        if listener is not None and hasattr(listener, "stop"):
            listener.stop()

    def is_active(self) -> bool:
        return (not self.enabled) or self._pressed.is_set()


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

        try:
            if text.startswith("/text "):
                await agent.submit_raw_text(
                    text.removeprefix("/text "), source="stdin-text"
                )
            else:
                await agent.submit_utterance(text, source="stdin")
        except Exception as exc:
            print(f"[agent] input submit failed: {exc}")
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
    import queue
    import sounddevice as sd

    config = MicConfig()
    selected_input = config.input_device if config.input_device is not None else "default"
    ptt_gate = PushToTalkGate(
        enabled=_env_bool("AGENT_MIC_PTT_ENABLED", True),
        key_name=os.getenv("AGENT_MIC_PTT_KEY", "alt_r"),
    )
    ptt_gate.start()

    print(f"[agent] microphone mode enabled (input={selected_input})")
    if ptt_gate.enabled:
        print(
            "[agent] push-to-talk enabled in Python process "
            f"(key={os.getenv('AGENT_MIC_PTT_KEY', 'alt_r')}, hold to transmit)"
        )
    else:
        print("[agent] push-to-talk disabled; microphone transmits without keyboard gating")

    try:
        if config.sample_rate < 24000:
            print(
                f"[agent] MIC_SAMPLE_RATE={config.sample_rate} is below realtime minimum; "
                "using 24000 for direct realtime audio input"
            )
            config.sample_rate = 24000

        submit_audio_input = getattr(agent, "submit_audio_input", None)
        if not callable(submit_audio_input):
            raise RuntimeError("Active runtime does not support direct audio input")

        print("[agent] streaming realtime audio input from computer microphone")
        audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=256)

        def _on_audio(indata, _frames, _time_info, status) -> None:
            if status or not ptt_gate.is_active():
                return
            mono = indata[:, 0].copy()
            chunk = mono.tobytes()
            try:
                audio_queue.put_nowait(chunk)
            except queue.Full:
                # Drop oldest-ish data under backpressure to keep stream realtime.
                pass

        with sd.InputStream(
            samplerate=config.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=config.frame_samples,
            device=config.input_device,
            callback=_on_audio,
        ):
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
    finally:
        ptt_gate.stop()


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
    runtime = os.getenv("AGENT_RUNTIME", "realtime").strip().lower() or "realtime"
    if runtime != "realtime":
        raise RuntimeError(
            "Pipecat runtime has been removed. Set AGENT_RUNTIME=realtime or unset AGENT_RUNTIME."
        )

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for realtime voice runtime")

    broadcaster = DisplayBroadcaster()
    from realtime_service.service import RealtimeClawVoiceService

    agent: AgentRuntime = RealtimeClawVoiceService(broadcaster, success_rate=success_rate)
    stop_event = asyncio.Event()

    try:
        await agent.start()
    except Exception as exc:
        if "invalid_model" in str(exc).lower():
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
        warned_ws_mic_ignored = False
        try:
            async for message in websocket:
                if isinstance(message, (bytes, bytearray, memoryview)):
                    if args.mic:
                        if not warned_ws_mic_ignored:
                            warned_ws_mic_ignored = True
                            print(
                                "[agent] ignoring websocket microphone audio while --mic "
                                "is active (using computer input device)"
                            )
                        continue
                    try:
                        submit_audio_input = getattr(agent, "submit_audio_input", None)
                        if callable(submit_audio_input):
                            await submit_audio_input(bytes(message), source="ws-mic")
                        elif not warned_binary_unsupported:
                            warned_binary_unsupported = True
                            print(
                                "[agent] received websocket binary audio, but this runtime "
                                "does not support direct audio input; ignoring chunks"
                            )
                    except Exception as exc:
                        print(f"[agent] websocket binary input failed: {exc}")
                    continue
                if not isinstance(message, str):
                    continue
                parsed = _parse_client_input(message)
                if not parsed:
                    continue
                input_type, text = parsed
                try:
                    if input_type == "raw_text":
                        await agent.submit_raw_text(text, source="ws-text")
                    else:
                        await agent.submit_utterance(text, source="ws")
                except Exception as exc:
                    print(f"[agent] websocket text input failed: {exc}")
        except (ConnectionClosed, OSError):
            pass
        finally:
            await broadcaster.remove_client(websocket)

    ws_server = await websockets.serve(handle_client, host, port)

    print(f"[agent] websocket listening on ws://{host}:{port}")
    print("[agent] runtime: realtime")
    print(f"[agent] success rate: {success_rate}")
    if args.demo:
        print(f"[agent] demo mode enabled ({demo_interval_ms}ms interval)")
    if args.mic:
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
    parser = argparse.ArgumentParser(description="Realtime-powered claw display agent")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Automatically enqueue demo utterances in a loop.",
    )
    parser.add_argument(
        "--mic",
        action="store_true",
        help="Capture real microphone input and stream directly to realtime model.",
    )
    return parser.parse_args(argv)


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
