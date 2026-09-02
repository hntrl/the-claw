set shell := ["bash", "-cu"]

default:
  @just --list

bootstrap:
  @pnpm --dir packages/web install
  @cd packages/agent && uv sync

bootstrap-web:
  @pnpm --dir packages/web install

bootstrap-agent:
  @cd packages/agent && uv sync

web-dev:
  @cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort

web-build:
  @cd packages/web && pnpm build

web-mock:
  @cd packages/web && pnpm mock:ws

agent-dev:
  @cd packages/agent && uv run python server.py

agent-realtime:
  @cd packages/agent && uv run python server.py

agent-demo:
  @cd packages/agent && uv run python server.py --demo

agent-mic:
  @cd packages/agent && uv run python server.py --mic

agent-realtime-mic:
  @cd packages/agent && uv run python server.py --mic

agent-audio-devices:
  @cd packages/agent && uv run python -c "import sounddevice as sd; print('default input/output device:', sd.default.device); [print(f'{i}: {d[\"name\"]} (max_in={d.get(\"max_input_channels\", 0)}, max_out={d.get(\"max_output_channels\", 0)}, default_sr={d.get(\"default_samplerate\")})') for i, d in enumerate(sd.query_devices())]"

demo:
  @agent_pid=''; web_pid=''; cleanup() { trap - EXIT INT TERM; [ -z "$agent_pid" ] || kill "$agent_pid" 2>/dev/null || true; [ -z "$web_pid" ] || kill "$web_pid" 2>/dev/null || true; wait; }; \
    (cd packages/agent && uv run python server.py) & agent_pid=$!; \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & web_pid=$!; \
    trap cleanup EXIT INT TERM; \
    wait

demo-realtime:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/agent && uv run python server.py) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait

demo-mic:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/agent && uv run python server.py --mic) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait

demo-realtime-mic:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/agent && uv run python server.py --mic) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait

demo-mock:
  @trap 'kill 0' EXIT INT TERM; \
    (cd packages/web && pnpm mock:ws) & \
    (cd packages/web && pnpm dev --host 127.0.0.1 --port 5173 --strictPort) & \
    wait

claw-command-install:
  @bash scripts/install-claw-command

service-install:
  @bash scripts/claw-service install

service:
  @bash scripts/claw-service restart

service-stop:
  @bash scripts/claw-service stop

service-status:
  @bash scripts/claw-service status

service-uninstall:
  @bash scripts/claw-service uninstall

service-logs:
  @bash scripts/claw-service logs
