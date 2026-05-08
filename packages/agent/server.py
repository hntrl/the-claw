from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress

import websockets
from dotenv import load_dotenv

from pipecat_service.microphone import MicConfig, OpenAITranscriber, listen_for_utterance
from pipecat_service.service import DisplayBroadcaster, PipecatClawVoiceService


DEMO_PHRASES: tuple[str, ...] = (
    "grab the green capsule near the front left corner",
    "move right then forward and drop on the blue duck",
    "pick up the red ball near the middle",
)


async def _stdin_loop(agent: PipecatClawVoiceService, stop_event: asyncio.Event) -> None:
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


async def _demo_loop(agent: PipecatClawVoiceService, stop_event: asyncio.Event, interval_ms: int) -> None:
    index = 0
    while not stop_event.is_set():
        phrase = DEMO_PHRASES[index % len(DEMO_PHRASES)]
        index += 1
        await agent.submit_utterance(phrase, source="demo")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_ms / 1000)
        except asyncio.TimeoutError:
            continue


async def _mic_loop(agent: PipecatClawVoiceService, stop_event: asyncio.Event) -> None:
    config = MicConfig()
    transcriber = OpenAITranscriber(config.transcribe_model)
    loop = asyncio.get_running_loop()

    print("[agent] microphone mode enabled")

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

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required (LLM tool-calling + optional mic STT)")

    broadcaster = DisplayBroadcaster()
    agent = PipecatClawVoiceService(broadcaster, success_rate=success_rate)
    stop_event = asyncio.Event()

    await agent.start()

    async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
        await broadcaster.add_client(websocket)
        await websocket.send(json.dumps({"type": "state", "state": "attract"}))

        try:
            async for message in websocket:
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
        finally:
            await broadcaster.remove_client(websocket)

    ws_server = await websockets.serve(handle_client, host, port)

    print(f"[agent] websocket listening on ws://{host}:{port}")
    print(f"[agent] success rate: {success_rate}")
    if args.demo:
        print(f"[agent] demo mode enabled ({demo_interval_ms}ms interval)")
    if args.mic:
        print(f"[agent] using model {os.getenv('OPENAI_TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe')} for mic STT")
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
        help="Capture real microphone utterances and transcribe with OpenAI.",
    )
    return parser.parse_args(argv)


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
