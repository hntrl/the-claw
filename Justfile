set shell := ["bash", "-cu"]

default:
  @just --list

bootstrap:
  @pnpm --dir packages/web install
  @cd packages/agent && uv sync
  @cd packages/old && uv sync

bootstrap-web:
  @pnpm --dir packages/web install

bootstrap-agent:
  @cd packages/agent && uv sync

bootstrap-old:
  @cd packages/old && uv sync

web-dev:
  @cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort

web-build:
  @cd packages/web && pnpm build

web-mock:
  @cd packages/web && pnpm mock:ws

agent-dev:
  @cd packages/agent && uv run python server.py

agent-demo:
  @cd packages/agent && uv run python server.py --demo

agent-mic:
  @cd packages/agent && uv run python server.py --mic

old-dev:
  @cd packages/old && uv run uvicorn agent.app:app --host $${AGENT_HOST:-127.0.0.1} --port $${AGENT_PORT:-8000} --reload

old-cli:
  @cd packages/old && uv run python scripts/demo_cli.py

demo:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/agent && uv run python server.py) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait

demo-mic:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/agent && uv run python server.py --mic) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait

demo-mock:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/web && pnpm mock:ws) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait
