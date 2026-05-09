# The Claw Monorepo

This repo is now organized as a lightweight monorepo:

- `packages/web/`: Vite + React display app
- `packages/agent/`: Python Pipecat/LangChain voice + tool orchestration service
- `packages/old/`: archived previous prototype/runtime

## One-command demo

Install and run with `just`:

```bash
cp .env.example .env
just bootstrap
just demo
```

That starts:

- agent websocket service on `ws://localhost:8787`
- web app on `http://localhost:5173`

Connect with:

```txt
http://localhost:5173/?ws=ws://localhost:8787
```

## Useful commands

```bash
just web-dev
just web-build
just web-mock
just agent-dev
just agent-realtime
just agent-demo
just agent-mic
just agent-realtime-mic
just old-dev
just old-cli
just demo-realtime
just demo-mic
just demo-realtime-mic
just demo-mock
```

## Package docs

- Web details: `packages/web/README.md`
- Agent details: `packages/agent/README.md`
- Legacy prototype: `packages/old/README.md`
