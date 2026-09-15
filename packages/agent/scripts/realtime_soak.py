"""Opt-in live Realtime soak with simulated hardware and silent, paced audio.

From packages/agent: .venv/bin/python scripts/realtime_soak.py --live
Uses the configured API credential/model and incurs normal API usage.
"""

import argparse
import asyncio
from collections import deque
import logging
import os
from pathlib import Path
import sys
from unittest.mock import patch

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.claw_controller import ClawControllerConfig  # noqa: E402
from realtime_service.service_v2 import RealtimeClawVoiceServiceV2  # noqa: E402


class SilentSpeaker:
    def __init__(self):
        self.bytes_played = 0

    async def write(self, audio, *, sample_rate, dtype, channels=1):
        assert dtype == "int16"
        await asyncio.sleep(len(audio) / (sample_rate * channels * 2))
        self.bytes_played += len(audio)

    async def close(self):
        pass


class DisplayProbe:
    def __init__(self):
        self.accepted = asyncio.Event()
        self.finished = asyncio.Event()
        self.errors = 0
        self.tool_completions = 0

    async def broadcast(self, event):
        if event["type"] == "transcript":
            self.accepted.set()
        if event == {"type": "state", "state": "attract"}:
            self.finished.set()
        if event == {"type": "state", "state": "error"}:
            self.errors += 1
        if event == {
            "type": "agent_step",
            "step": "claw_execute",
            "status": "complete",
        }:
            self.tool_completions += 1


class ResponseProbe:
    def __init__(self):
        self.recent = deque(maxlen=12)

    async def on_event(self, event):
        if (
            event.type != "raw_server_event"
            or event.data.get("type") != "response.done"
        ):
            return
        response = event.data["response"]
        self.recent.append(
            {
                "status": response.get("status"),
                "turn": (response.get("metadata") or {}).get("claw_turn_id"),
                "outputs": [item.get("type") for item in response.get("output", [])],
            }
        )


async def run(args):
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
    os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    speaker = SilentSpeaker()
    display = DisplayProbe()
    # Construct a sim config directly: even USB discovery is bypassed.
    with (
        patch(
            "realtime_service.service_v2.ClawControllerConfig.from_env",
            return_value=ClawControllerConfig(mode="sim"),
        ),
        patch("realtime_service.service_v2.LocalAudioOutput", return_value=speaker),
        patch.dict(os.environ, {"AGENT_REALTIME_PLAY_AUDIO": "1"}),
    ):
        service = RealtimeClawVoiceServiceV2(display)
    started = asyncio.get_running_loop().time()
    turns = 0
    probe = ResponseProbe()
    commands = (
        "Open the claw, then close it. Give a very brief spoken confirmation.",
        "Set your expression to excited, then get_state. Give a very brief spoken status.",
    )
    try:
        await asyncio.wait_for(service.start(), timeout=30)
        initial_session = service._session
        initial_session.model.add_listener(probe)
        while True:
            display.accepted.clear()
            display.finished.clear()
            audio_before = speaker.bytes_played
            await service.submit_raw_text(commands[turns % len(commands)])
            await asyncio.wait_for(display.accepted.wait(), timeout=10)
            await asyncio.wait_for(display.finished.wait(), timeout=45)
            turns += 1
            assert display.errors == 0, "Service emitted an error"
            assert speaker.bytes_played > audio_before, (
                "Turn ended without spoken output"
            )
            assert service._session is initial_session, (
                "Session was unexpectedly replaced"
            )
            assert not service._events.done(), "Event bridge died"
            assert not service._coordinator._task.done(), "Command worker died"
            elapsed = asyncio.get_running_loop().time() - started
            print(
                f"elapsed={elapsed:.1f}s turns={turns} tool_completions={display.tool_completions} audio_seconds={speaker.bytes_played / 48000:.1f}",
                flush=True,
            )
            if elapsed >= args.duration:
                break
            await asyncio.sleep(args.interval)
        print(
            "PASS: same live session remained responsive past requested duration",
            flush=True,
        )
    except AssertionError as exc:
        print(
            f"Assertion: {exc}; recent response shapes: {list(probe.recent)}",
            flush=True,
        )
        raise
    finally:
        await service.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Allow real API calls (billable)"
    )
    parser.add_argument("--duration", type=float, default=210)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "Pass --live to opt in to real API usage; offline tests need no credential"
        )
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
