# Realtime vs Legacy Agent Reconciliation

## Scope
This document records how the prior prototype's behavior maps onto `packages/agent/realtime_service/service.py` and explains intentional departures.

## Tool Contract

| Prior prototype tool | Realtime tool | Status |
|---|---|---|
| `move_axis(axis, degrees)` | `move_axis(axis, degrees)` | Preserved |
| `open_claw(angle?)` | `open_claw(angle?)` | Preserved |
| `lower_claw(degrees?)` | `lower_claw(degrees?)` | Preserved |
| `raise_claw()` | `raise_claw()` | Preserved |
| `close_claw(angle?)` | `close_claw(angle?)` | Preserved |
| `home_z()` | `home_z()` | Preserved |
| `home()` | `home()` | Preserved |
| `get_state()` | `get_state()` | Preserved |
| `halt()` | `halt()` | Preserved |
| `reset_emergency()` | `reset_emergency()` | Preserved |
| _(none)_ | `set_expression(mood, emotion?)` | Intentional extension |

### Why `set_expression` remains
- Realtime agent must drive frontend sprite mood over websocket in-frame.
- This is display-only, separated from hardware tooling.
- It does not alter claw controller state or serial protocol behavior.

## Prompt Contract

### Preserved from legacy
- ClawPilot persona (short, theatrical).
- Axis/sign conventions.
- Degree-based physical guidance.
- Homing and emergency semantics (`halt`, `home_z`, `home`).
- Handling expectations for `LIMIT` and `STUCK` statuses.
- Guidance to suggest Z-homing when `z_homed` is false for depth-sensitive asks.

### Intentional departures
1. The realtime prompt explicitly includes `set_expression` as optional display control.
2. The prompt is shorter than the legacy FastAPI prompt block to keep realtime audio turns responsive and avoid overlong speech planning context.
3. Realtime audio path cannot reliably rewrite the exact just-captured user utterance before model response every turn; instead:
   - text-initiated turns prepend a machine-status banner, and
   - tooling includes `get_state()` for fresh state checks in-LLM.

## Execution/State Behavior

### Preserved
- Tool results include state snapshots (`state`) alongside command status (`ok`, `status`, `command`, `error`, `events`) via the shared `packages/agent/common/claw_controller` module.
- Real/noop backend abstraction remains, both routed through legacy protocol semantics.

### Intentional departures
1. Realtime service emits frame-driven frontend events (`agent_step`, `state`, `claw_motion`, `effect`) around tool calls.
   - Justification: web UI is event-driven and needs deterministic animation/state transitions.
2. Legacy orchestration tools (`parse_intent`, `select_target`, `plan_motion`, `execute_claw`) are removed from realtime.
   - Justification: those were demo abstractions and diverged from actual hardware contract.

## Files Updated
- `packages/agent/realtime_service/service.py`
- `packages/agent/common/claw_controller/controller.py`
- `packages/agent/common/firmware/claw_machine_V4_agent/claw_machine_V4_agent.ino`
