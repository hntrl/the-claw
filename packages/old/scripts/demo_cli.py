from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000")


def main() -> None:
    print("Claw Demo CLI")
    print("Type commands as if they were transcribed voice input. Type 'quit' to exit.")
    session_id = "demo-session"
    while True:
        text = input("\nYou> ").strip()
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            break
        resp = httpx.post(
            f"{AGENT_BASE_URL}/turn",
            json={"text": text, "session_id": session_id},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"Agent> {data['reply']}")
        if data["tool_events"]:
            print("Tools:")
            for ev in data["tool_events"]:
                print(f"- {ev['tool']} {ev['args']}")


if __name__ == "__main__":
    main()

