# Realtime v2 reliability investigation

The reported symptom was needing to restart the program approximately every
three minutes. We reproduced several failures without hardware, including a
process that keeps accepting input after its command worker or event consumer
has died. This establishes concrete bugs, but does not establish which event
triggered the original field failure or why it happened at that interval.

OpenAI documents a [60-minute maximum Realtime session duration](https://developers.openai.com/api/docs/guides/realtime-conversations#session-lifecycle-events),
not a three-minute limit.

## Reproduced failures and fixes

| Trigger | Original behavior | Fix |
| --- | --- | --- |
| Socket send raises once | Exception escapes the turn callback and permanently kills `TurnCoordinator._run`; later input is queued with no consumer. | Handle the failed send, invalidate and close the failed session, and retain the command worker for later input. Never automatically replay an uncertain hardware command. |
| Socket receive fails | The SDK raises from its async iterator. The service only handled `error` events, so its event task dies while `_session` remains non-null. | Catch iterator failures and close the specific failed session; later input creates a new session. |
| Speaker write raises or stalls | An exception kills the event bridge; an unbounded write can prevent it from consuming control events. | Honor the audio-enable and write-timeout settings; isolate speaker failures and disable output while continuing model/tool processing. |
| A tool call ends its model response | `agent_end` plus `tool_end` marks the entire user turn complete before the SDK's tool-result continuation. Its speech is dropped and further tools can be rejected. | Finish on a final `response.done` without function calls, not on the SDK's per-response `agent_end`. |
| Old response ends after replacement input | The old event finishes whichever turn is current. This also occurs if the first response acknowledgment arrives after replacement input. | Tag outgoing responses with `claw_turn_id` metadata and check ownership on receipt and completion. |
| Old tool waits for the controller lock during replacement | The eligibility callback checks whether *any* turn is active, so the old tool can execute as part of the new turn. | Bind tool-call IDs to their originating turn and propagate that identity through the controller wait and display events; suppress stale continuations. |

Session startup is serialized, failed connection setup is cleaned up before the
session becomes visible, and reconnecting reuses the controller. A benign
`response_cancel_not_active` error preserves the session. Failed/incomplete
responses now produce an error display and a log containing the status/error
code. Error logging omits user text and credential values.

## Offline reproduction

From `packages/agent`:

```sh
.venv/bin/python -m unittest discover -s tests -p test_service_v2.py -v
```

The replay uses the installed Agents SDK, including its actual WebSocket event
parser, session iterator, asynchronous tool dispatch, and response sequencer.
Only the socket, controller/config, and speaker are replaced. It does not load
credentials, contact the API, enumerate USB devices, or open audio/serial devices.

The initial five regression cases all failed before the fix. Additional cases
cover interrupted tools, replacement before response acknowledgment, a hanging
speaker, concurrent startup, recoverable cancellation, rate-limited responses,
multiple tool results sharing one continuation, and 50
consecutive turns containing 100 tool calls and 50 audio responses.

Validated with the lockfile versions: `openai-agents==0.20.0`, `openai==2.54.0`,
and `websockets==15.0.1`. The full Python suite contains 22 passing tests.

## Live reproduction without devices

```sh
.venv/bin/python scripts/realtime_soak.py --live --duration 210
```

This opt-in script uses the configured OpenAI API credential/model and incurs
normal API usage. It constructs the real controller in simulation mode, bypasses
USB discovery, and replaces local audio output with a silent sink paced at
24-kHz PCM playback speed. It checks that commands finish with audio, that the
same session survives, and that both background tasks remain alive. Logs contain
counts and elapsed times, not prompts, audio, or credentials.

An initial build with the recovery fixes completed 11 turns over 221.3 seconds
on one session without a reset. A subsequent turn-ownership implementation
failed the speech assertion in a live run. Replaying multiple tools from the
same response exposed redundant continuation requests in that implementation;
the final adapter batches all results from one response into one continuation.
A faster stress run also observed an actual `rate_limit_exceeded` response.
This is evidence of the API failure mode, not proof that rate limiting caused
the original field incident. Its recovery behavior is covered by replay.

The final build passed a live run lasting **231.5 seconds (3 minutes 51.5 seconds)**:
8 completed turns, 8 physical-tool completions, and 50.3 seconds of silently
consumed audio, all on the same session with no errors or process restart.
That run used `--duration 210 --interval 25`; the script finishes the first full
turn after the requested minimum duration rather than cutting off an active turn.

Live validation checks ordinary tool conversations; injected failure
and interruption scenarios are covered by deterministic replay. Neither test
exercises an actual audio driver, serial link, or the original network conditions.

Recovery creates a fresh conversation after a fatal session failure. It does
not preserve the failed session's conversation history. After a speaker failure,
tools/text remain usable but speaker output stays disabled until the service is
restarted.
