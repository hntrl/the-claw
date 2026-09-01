# The Claw Monorepo

This repo is now organized as a lightweight monorepo:

- `packages/web/`: Vite + React display app
- `packages/agent/`: Python realtime voice + tool orchestration service
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

## Run at macOS login

macOS uses `launchd` rather than systemd. Install the per-user launch agent once:

```bash
just service-install
```

It starts `just demo` after this user signs in and keeps it running if it exits.
Use this command whenever you want to start it (if stopped) or restart it:

```bash
just service
```

Install an operator-friendly `claw` command that works from any directory:

```bash
just claw-command-install
```

After that, the operator can use:

```bash
claw start
claw restart
claw stop
claw status
claw logs
claw open
claw help
```

The field-debug panel and debug keyboard shortcuts stay hidden by default. Add
`&debug` to the display URL (for example,
`http://127.0.0.1:5173/?ws=ws://127.0.0.1:8787&debug`) to enable them.

Additional controls:

```bash
just service-status
just service-stop
just service-logs
just service-uninstall
```

Logs are written to `~/Library/Logs/the-claw-demo.log` and
`~/Library/Logs/the-claw-demo.error.log`. The launch agent is intentionally a
per-user service, so the demo can access the signed-in user's audio and device
permissions. A macOS LaunchDaemon can start before login, but is not suitable
for this interactive hardware/audio demo.

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
