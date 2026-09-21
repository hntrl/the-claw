from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from collections.abc import Sequence
from contextlib import suppress
from typing import Protocol

import websockets
from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed

from common.broadcaster import DisplayBroadcaster
from realtime_service.service_v2 import RealtimeClawVoiceServiceV2

DEMO_PHRASES = (
    "grab the green capsule near the front left corner",
    "move right then forward and drop on the blue duck",
    "pick up the red ball near the middle",
)


class AgentRuntime(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit_utterance(self, text: str, *, source: str = "text") -> None: ...
    async def submit_raw_text(self, text: str, *, source: str = "text") -> None: ...


def _parse_client_input(message: str) -> str | None:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return message.strip() or None
    if not isinstance(payload, dict) or payload.get("type") not in {
        "raw_text",
        "utterance",
    }:
        return None
    text = payload.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


async def _demo_loop(
    agent: AgentRuntime, stop_event: asyncio.Event, interval_ms: int
) -> None:
    index = 0
    while not stop_event.is_set():
        await agent.submit_utterance(
            DEMO_PHRASES[index % len(DEMO_PHRASES)], source="demo"
        )
        index += 1
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), interval_ms / 1000)


async def run(args: argparse.Namespace) -> None:
    load_dotenv(override=False)
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for realtime runtime")
    host = os.getenv("AGENT_WS_HOST", "0.0.0.0")
    port = int(os.getenv("AGENT_WS_PORT", os.getenv("PORT", "8787")))
    broadcaster = DisplayBroadcaster()
    agent: AgentRuntime = RealtimeClawVoiceServiceV2(
        broadcaster, success_rate=float(os.getenv("AGENT_SUCCESS_RATE", "0.68"))
    )
    await agent.start()
    stop_event = asyncio.Event()

    async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
        await broadcaster.add_client(websocket)
        input_tasks: set[asyncio.Task[None]] = set()

        def report_input_task(task: asyncio.Task[None]) -> None:
            input_tasks.discard(task)
            if task.cancelled():
                return
            try:
                task.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] websocket text input failed: {exc}")

        warned_binary = False
        try:
            await asyncio.wait_for(
                websocket.send(json.dumps({"type": "state", "state": "attract"})),
                timeout=2.0,
            )
            async for message in websocket:
                if not isinstance(message, str):
                    if not warned_binary:
                        print(
                            "[agent] ignoring binary websocket input; text input is required"
                        )
                        warned_binary = True
                    continue
                text = _parse_client_input(message)
                if text:
                    task = asyncio.create_task(
                        agent.submit_raw_text(text, source="ws-text")
                    )
                    input_tasks.add(task)
                    task.add_done_callback(report_input_task)
        except (ConnectionClosed, OSError, asyncio.TimeoutError):
            pass
        finally:
            for task in input_tasks:
                task.cancel()
            await asyncio.gather(*input_tasks, return_exceptions=True)
            await broadcaster.remove_client(websocket)

    ws_server = await websockets.serve(handle_client, host, port)
    print(f"[agent] websocket listening on ws://{host}:{port}")
    print(
        f"[agent] realtime model: {os.getenv('OPENAI_REALTIME_MODEL', 'gpt-realtime-2.1')}"
    )
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    tasks = (
        [asyncio.create_task(_demo_loop(agent, stop_event, args.demo_interval_ms))]
        if args.demo
        else []
    )
    try:
        await stop_event.wait()
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        ws_server.close()
        await ws_server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await agent.stop()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Text-driven realtime claw display agent"
    )
    parser.add_argument(
        "--demo", action="store_true", help="Automatically enqueue demo text commands."
    )
    parser.add_argument(
        "--demo-interval-ms",
        type=int,
        default=int(os.getenv("AGENT_DEMO_INTERVAL_MS", "14000")),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
