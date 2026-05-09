# Claw Controller Package

Shared controller package used by agent runtimes.

This package is now based directly on the legacy hardware controller from
`packages/old/agent/claw_controller.py` (Arduino firmware ACK/DONE/STATE loop,
reader thread, command correlation, and sim fallback), wrapped behind a
runtime-facing facade that preserves the old direct hardware tools and also
supports the newer `execute_plan(...)` API.

The Arduino firmware now lives in this package at
`firmware/claw_machine_V4_agent/claw_machine_V4_agent.ino`. The sketch is an
unchanged copy of the tested legacy firmware from
`packages/old/claw_machine_V4_agent.ino` so the serial contract remains stable.

## Responsibilities

- Own controller state (connected/busy/last turn/error)
- Own per-turn execution state (target, motions, status, timestamps)
- Bind motion intents to concrete transport commands
- Support both simulation and Arduino serial transport
- Preserve direct hardware tool methods for realtime agents
- Provide `execute_plan(...)` for plan-based runtimes

## API

- `ClawControllerBackend` protocol (standard backend interface)
- `NoopClawControllerBackend` (no hardware, UI-safe simulation behavior)
- `LegacyProtocolClawControllerBackend` (flushes motion to legacy Arduino protocol)
- `ClawControllerConfig.from_env(...)`
- `ClawController.start()` / `stop()` facade that selects backend by mode
- `ClawController.execute_plan(target_label, motions, should_interrupt, on_event)`
- Old-compatible methods: `move_axis`, `open_claw`, `lower_claw`,
  `raise_claw`, `close_claw`, `home_z`, `home`, `halt`, `reset_emergency`,
  `get_state`
- `ClawController.snapshot()`

## Transport Modes

- `CLAW_CONTROLLER_MODE=sim` forces simulation
- `CLAW_CONTROLLER_MODE=serial` forces Arduino serial
- `CLAW_CONTROLLER_MODE=auto` tries serial, falls back to sim

Serial mode expects `pyserial` to be installed in the environment.

## Firmware

Open `firmware/claw_machine_V4_agent/claw_machine_V4_agent.ino` in the Arduino
IDE or build/upload it with the Arduino CLI. The sketch targets Arduino Uno +
CNC Shield V3 and requires:

- `AccelStepper`
- `Servo`

The firmware speaks the tested line-oriented serial protocol at `115200` baud:

- Host commands are ASCII lines, optionally prefixed with `#<id>`.
- Accepted commands emit `ACK <id> <cmd>`.
- Motion/instant completion emits `DONE <id> <cmd> <status>`.
- Rejections emit `ERR <id> <reason>`.
- Intermediate updates emit `EVT <event> ...`.
- State polling uses `STATE?` and returns `STATE <id> <fsm> <k=v> ...`.

The Python serial backend depends on those exact response prefixes for command
correlation and cached state updates.

Recommended hardware environment:

- `CLAW_CONTROLLER_MODE=serial`
- `CLAW_SERIAL_PORT=/dev/tty.usbmodem...` or the board-specific serial port
- `CLAW_SERIAL_BAUD=115200`

## Default Motion Mapping

- `left` -> `Y -CLAW_MOVE_Y_DEGREES`
- `right` -> `Y +CLAW_MOVE_Y_DEGREES`
- `forward` -> `X +CLAW_MOVE_X_DEGREES`
- `back` -> `X -CLAW_MOVE_X_DEGREES`
- `down` -> `Z +CLAW_MOVE_Z_DOWN_DEGREES`
- `up` -> `RAISE` (or `Z -CLAW_MOVE_Z_UP_DEGREES` when `CLAW_USE_RAISE_FOR_UP=0`)
