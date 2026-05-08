#!/usr/bin/env bash
set -euo pipefail

HOST="${AGENT_HOST:-127.0.0.1}"
PORT="${AGENT_PORT:-8000}"
URL="http://${HOST}:${PORT}/"

echo "Starting unified claw demo app on ${URL}"
uv run uvicorn agent.app:app --host "${HOST}" --port "${PORT}" --reload &
SERVER_PID=$!

cleanup() {
  kill "${SERVER_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

sleep 1
if command -v open >/dev/null 2>&1; then
  open "${URL}" || true
fi

wait "${SERVER_PID}"
