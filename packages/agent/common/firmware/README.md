# Claw Controller Firmware

This directory contains the Arduino firmware used by the shared
`common/claw_controller` Python module in `packages/agent`.

## Sketch

- `claw_machine_V4_agent/claw_machine_V4_agent.ino`

The sketch is copied unchanged from the old tested implementation at
`packages/old/claw_machine_V4_agent.ino`. Keep protocol changes deliberate:
the Python backend parses `READY`, `ACK`, `DONE`, `ERR`, `EVT`, `STATE`, and
`PONG` lines and correlates commands by optional `#<id>` prefixes.

## Hardware Target

- Arduino Uno
- CNC Shield V3
- A4988 stepper drivers
- `AccelStepper` Arduino library
- `Servo` Arduino library
- Serial baud: `115200`

## Runtime Pairing

Use serial mode in the Python runtime when driving the physical machine:

```sh
export CLAW_CONTROLLER_MODE=serial
export CLAW_SERIAL_PORT=/dev/tty.usbmodem...
export CLAW_SERIAL_BAUD=115200
```

Use `CLAW_CONTROLLER_MODE=auto` during development if falling back to the
simulation backend is acceptable when the board is disconnected.
