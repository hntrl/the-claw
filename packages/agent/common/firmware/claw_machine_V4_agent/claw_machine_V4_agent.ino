#include <AccelStepper.h>
#include <Servo.h>

// ============================================================================
// CLAW MACHINE CONTROLLER — AGENT-FRIENDLY PROTOCOL (V2_agent)
// For use with CNC Shield V3 + Arduino Uno
//
// Motor mapping (unchanged from V2):
//   X gantry left:  X slot (D2/D5)
//   X gantry right: A slot (D12/D13) - independent control for leveling
//   Y gantry:       Y slot (D3/D6)
//   Z axis:         Z slot (D4/D7) - vertical claw movement
//   Claw servo:     Pin A3 (external power, common GND to Arduino)
//
// Limit switches (normally open, wired pin-to-GND, internal pullup):
//   X: Pin 9  (both ends OR'd together)
//   A: Pin 10 (both ends OR'd together)
//   Z: Pin 11 (top position only)
//
// ----------------------------------------------------------------------------
// PROTOCOL OVERVIEW
//
// All commands are single lines terminated by '\n'.
// Every command that is accepted gets an immediate ACK, and every command
// that involves motion emits a DONE (or ERR) when the action fully completes.
// Servo / instant commands (OPEN, CLOSE, S, PING, STATE?) emit ACK + DONE
// back-to-back because their "completion" is immediate from the protocol's
// point of view — the Python layer can still poll STATE? to watch the servo
// sweep if it cares about fine-grained progress.
//
// Optional command IDs: prefix a command with "#<id> " to have the ID echoed
// in ACK/DONE/ERR for that command. If omitted, ID defaults to "-".
//
// Commands:
//   X <degrees>        Move X gantry (both motors) by degrees. Stops on limit.
//   Y <degrees>        Move Y by degrees.
//   Z <degrees>        Move Z by degrees. Positive = down, negative = up.
//                      Hard bottom bound Z <= Z_MAX_DOWN_DEGREES (clamped,
//                      EVT Z_CLAMPED).
//                      Top bound is HARDWARE: the Z_TOP_LIMIT switch
//                      (Arduino A0) auto-stops upward motion. On hit, Z is
//                      resynced to 0 at the switch trigger point and the
//                      claw backs off by Z_BACKUP_DEGREES so it isn't
//                      resting on the switch.
//   ZHOME              Drive Z upward until Z_TOP_LIMIT fires; resync
//                      Z=0 at the switch, then back off by
//                      Z_HOME_BACKOFF_DEGREES so the claw parks clear of
//                      the (delicate) limit switch. Times out with
//                      DONE ZHOME STUCK after HOME_PHASE_TIMEOUT_MS.
//   RAISE              Equivalent to ZHOME — drives up until the top
//                      limit fires. Kept as a separate name for backward
//                      compatibility / readability in the agent.
//   OPEN [angle]       Set servo to open (default SERVO_OPEN_ANGLE).
//   CLOSE [angle]      Set servo to closed (default SERVO_CLOSED_ANGLE).
//   S <angle>          Set servo to absolute angle (clamped to physical limits).
//   HOME               Run full homing sequence: Z up to limit (resync +
//                      backoff), then X+A homing, then Y homing, then
//                      claw open.
//   HALT               Immediately stop all motion. Abort any in-progress command.
//   STATE?             Emit one-line state report.
//   PING               Emit PONG.
//   EN                 Enable motors (drivers on).
//   DIS                Disable motors (drivers off — gantry may fall!).
//
// Response types (first token always identifies the line):
//   READY                        — Boot complete, firmware alive.
//   INFO <text>                  — Free-form informational.
//   ACK <id> <cmd>               — Command accepted, starting.
//   DONE <id> <cmd> <status>     — Command complete. Status: OK | LIMIT | TIMEOUT | HALTED | STUCK
//   ERR <id> <reason>            — Command rejected. Reason: BUSY | UNKNOWN | BAD_ARGS
//   EVT <event> <args>           — Intermediate event (limit hit, backoff, homing step, clamp)
//   STATE <k=v> ...              — State report (response to STATE?)
//   PONG                         — Response to PING.
// ============================================================================

// === PIN DEFINITIONS (CNC Shield V3) ===

#define EN 8  // Enable pin (active LOW = enabled, HIGH = disabled)

// X GANTRY LEFT: X slot
#define X_STP 2
#define X_DIR 5

// X GANTRY RIGHT: A slot (independent via D12/D13 jumpers removed)
#define A_STP 12
#define A_DIR 13

// Y GANTRY: Y slot
#define Y_STP 3
#define Y_DIR 6

// Z AXIS: Z slot
#define Z_STP 4
#define Z_DIR 7

// Limit switch pins (wire between signal pin and GND, normally open)
// Limit switch pins (wire between signal pin and GND, normally open).
//
// IMPORTANT: the shield pin LABELS no longer match the motor they belong to.
// The Arduino-side pin numbers are what the firmware cares about; the shield
// labels are cosmetic. Actual mapping:
//
//   Shield labels   Arduino pin   Logical use
//   ─────────────   ───────────   ───────────────────────────────────────
//   X+/X-           pin 9         X motor — both ends OR'd to one signal
//   Y+/Y-           pin 10        A motor — both ends OR'd to one signal
//   Z+/Z-           pin 11        Y motor — both ends OR'd to one signal
//   Abort           A0            Z TOP limit (defined separately below)
//
// All limit pins are read with INPUT_PULLUP; the switch closes the line to
// GND when triggered, so logic-LOW means "limit hit."
#define X_LIMIT 9     // X motor (X gantry left), both ends OR'd
#define A_LIMIT 10    // A motor (X gantry right), both ends OR'd
#define Y_LIMIT 11    // Y motor, both ends OR'd (was Z_LIMIT in earlier builds)

// Z TOP limit switch — wired to the CNC Shield's Abort header (Arduino A0).
// Fires LOW when the claw reaches the top of travel. This is the
// mechanical truth of "Z = 0" and is the SOURCE OF TRUTH for Z position;
// stepper-step counting is treated as best-effort and is auto-corrected
// any time the switch fires during an upward move. Same INPUT_PULLUP
// convention as the X/Y/A switches: closing the contact pulls the line
// to GND.
//
// Why a hardware top limit instead of step-counting:
// On a cable spool, the Z motor can pay out slack if the claw is held up
// or descends past the cable's natural travel. When the user then asks
// the claw to retract, the motor spins freely while reeling slack in,
// then encounters a sudden tension shock when the slack runs out — which
// causes step-skipping. With a hardware top limit, the loss of step
// fidelity is irrelevant: the switch defines Z=0 and the firmware
// resyncs to it on every retract that reaches the top.
//
// The Z top switch is also delicate, so after every hit we drop the claw
// a small amount (Z_BACKUP_DEGREES on plain Z moves, Z_HOME_BACKOFF_DEGREES
// on homing paths) so it isn't resting against / torquing the switch.
#define Z_TOP_LIMIT A0

// Servo pin — A3 is clean: no CNC Shield conflicts, Servo lib uses Timer1
#define CLAW_SERVO_PIN A3

// === PROTOCOL CONFIG ===

#define SERIAL_BAUD 115200
#define CMD_BUFFER_SIZE 48

// === CONFIGURATION ===

// ============================================================================
// ┌─ EDIT HERE: Z-AXIS POSITION MODEL ────────────────────────────────────┐
// │                                                                       │
// │  HARDWARE-DEFINED CEILING:                                            │
// │                                                                       │
// │  Z = 0 is whatever motor position the Z_TOP_LIMIT switch reports as   │
// │  pressed. The switch is the source of truth, not step counting. Any   │
// │  upward Z move that fires the switch resyncs zPositionSteps = 0,      │
// │  correcting any cumulative step-skip drift from cable-spool tension   │
// │  shocks or other transient missed steps. After resync, the claw       │
// │  backs off a small amount so it isn't pressing against the switch.    │
// │                                                                       │
// │  Z position is tracked in motor degrees from the switch-defined Z=0:  │
// │    Z = 0    -> claw at the top limit (the switch trigger point)       │
// │    Z > 0    -> claw lower (descended by this many degrees)            │
// │                                                                       │
// │  After any homing path (HOME / ZHOME / RAISE), the claw rests at      │
// │  Z = Z_HOME_BACKOFF_DEGREES, NOT at Z = 0. The switch trigger point   │
// │  is the canonical zero and Z values are referenced to it.             │
// │                                                                       │
// │  POSITION-KNOWN FLAG:                                                 │
// │                                                                       │
// │  At boot, the firmware doesn't know where Z is unless the switch      │
// │  happens to be pressed. zPositionKnown is checked at boot and updated │
// │  whenever the top switch fires. Until it's true, the agent should     │
// │  ZHOME first to establish a real reference; Z motion still works      │
// │  during this period, but reported Z values are best-effort.           │
// │                                                                       │
// │  Z BOUNDS:                                                            │
// │                                                                       │
// │  TOP: HARDWARE switch only. No software top clamp. The switch fires   │
// │     during upward motion, motor hard-stops, Z resyncs to 0, and the   │
// │     claw drops by the relevant backoff amount.                        │
// │                                                                       │
// │  HARD MAX (Z_MAX_DOWN_DEGREES): HARD safety bound on descent.         │
// │     Set this to JUST UNDER the cable length so the spool can't fully  │
// │     unwind (which would let the motor start re-winding in the other   │
// │     direction and damage the spool/cable).                            │
// │     Currently 2160 degrees = 6 feet of cable at the current spool     │
// │     diameter. Out-of-range descents are clamped, with EVT Z_CLAMPED   │
// │     emitted.                                                          │
// │                                                                       │
// │  SOFT REFERENCE (Z_REFERENCE_DOWN_DEGREES): purely informational.     │
// │     Reported in STATE. Represents "approximately a default drop" for  │
// │     the agent's awareness but does NOT enforce anything.              │
// │     Currently 1080 degrees = 3 feet (the configured default drop).    │
// │                                                                       │
// │  TUNING:                                                              │
// │    360 degrees ≈ 1 foot of cable at the current spool diameter.       │
// │    The Python default for unspecified "lower" is set separately in    │
// │    DEFAULT_LOWER_DEGREES (in agent/app.py).                           │
// │                                                                       │
// └───────────────────────────────────────────────────────────────────────┘
// Z reach: 6 feet of extension below the ceiling (Z=0). At ~360 deg/ft on the
// current spool, that's 2160 degrees of motor travel. Z_REFERENCE is the
// default drop depth (3 feet), used for informational STATE reporting.
const float Z_MAX_DOWN_DEGREES       = 1080.0;  // WAS 2160 and had overrotation issues.  HARD safety: 6 feet of cable
const float Z_REFERENCE_DOWN_DEGREES = 800.0;  // WAS 1080. informational, ~2 feet

// Z direction sign: set to +1 if positive AccelStepper steps physically
// move the claw DOWN. Set to -1 if positive steps move it UP. The agent
// model is "positive Z = down" — this constant translates to whatever the
// motor wiring actually does. Flipping the Z DIR pin connector or this
// constant are equivalent fixes.
const int   Z_DOWN_SIGN = +1;            // flip to -1 if lowering goes up

// Steps per revolution (200 steps/rev * 16 microsteps)
const float STEPS_PER_REVOLUTION = 700.0;  //was 3200 before
const float STEPS_PER_DEGREE = STEPS_PER_REVOLUTION / 360.0;

// Top of travel — hard bound. Always 0 in agent frame.
const long Z_MIN_STEPS = 0;

// Bottom of travel — hard SAFETY bound, derived from Z_MAX_DOWN_DEGREES.
const long Z_MAX_STEPS = (long)(Z_MAX_DOWN_DEGREES * STEPS_PER_DEGREE);

// Informational reference depth, in steps. Reported in STATE for context.
// NOT a bound — set independently from the hard max.
const long Z_REFERENCE_DOWN_STEPS = (long)(Z_REFERENCE_DOWN_DEGREES * STEPS_PER_DEGREE);

// Backup amounts (after hitting limit switches during normal X motion)
const float X_BACKUP_DEGREES = 15.0;
const long  X_BACKUP_STEPS = (long)(X_BACKUP_DEGREES * STEPS_PER_DEGREE);

// Y backup amount when a Y limit switch fires (matches X behavior).
const float Y_BACKUP_DEGREES = 25.0;
const long  Y_BACKUP_STEPS = (long)(Y_BACKUP_DEGREES * STEPS_PER_DEGREE);

// Z backup amount when the top limit switch fires during a plain Z move.
// Modest — gets the claw clear of the switch but doesn't waste much travel.
const float Z_BACKUP_DEGREES = 25.0;
const long  Z_BACKUP_STEPS = (long)(Z_BACKUP_DEGREES * STEPS_PER_DEGREE);

// Small post-home backoff to keep the claw from resting against the
// (delicate) Z top limit switch after homing. The Z=0 reference is still
// the switch trigger point itself; after homing the claw simply parks at
// Z = Z_HOME_BACKOFF_DEGREES. Keep this small.
const float Z_HOME_BACKOFF_DEGREES = 10.0;
const long  Z_HOME_BACKOFF_STEPS = (long)(Z_HOME_BACKOFF_DEGREES * STEPS_PER_DEGREE);

// Homing / alignment configuration
//
// Sequence (all four phases run on a single HOME command):
//   1. X + A both drive toward their limit switches. Each motor stops as
//      soon as ITS OWN switch fires; the other keeps going until either
//      its switch fires or HOME_TIMEOUT_MS elapses.
//   2. Both back off together by HOME_BACKOFF_TURNS revolutions, in the
//      direction opposite the homing drive.
//   3. Y drives toward its limit (which side depends on HOME_Y_DIRECTION).
//   4. Y backs off by HOME_Y_BACKOFF_TURNS, then the claw servo opens.
//
// The Y limit pin is OR'd between both physical switches, so "drive until
// it hits something" works for either direction — only the sign of the
// commanded move differs.
//
// HOMING IS ALSO USED AS DELIVERY: a HOME at session start reliably parks
// the claw at the front-left corner where the dropoff slot lives. So when
// the user asks "bring it to me," the agent just calls home(). If home is
// driving the wrong way after physical testing, FLIP THE TWO CONSTANTS
// BELOW — they're the only place homing direction is encoded.

// ┌─ EDIT HERE: HOMING DIRECTION SWITCHES ─────────────────────────────┐
// │                                                                    │
// │  Each constant is +1 or -1. Flip the sign to reverse which side    │
// │  the gantry homes to. After flipping, the corresponding back-off   │
// │  direction inverts automatically.                                  │
// │                                                                    │
// │    HOME_X_DIRECTION = +1  -> X+A drive in +X direction (toward     │
// │                              whichever side that is on your build) │
// │    HOME_X_DIRECTION = -1  -> X+A drive in -X direction             │
// │                                                                    │
// │  Same convention for Y. Goal of current setup: park claw at the    │
// │  front-left corner where the dropoff slot is.                      │
// │                                                                    │
// └────────────────────────────────────────────────────────────────────┘
const int HOME_X_DIRECTION = -1;
const int HOME_Y_DIRECTION = -1;

const float HOME_DEGREES = 200.0 * 360.0;  // Large travel to guarantee hitting limit
const long  HOME_STEPS = (long)(HOME_DEGREES * STEPS_PER_DEGREE);
const float HOME_BACKOFF_TURNS = 0.1;      // Back off 1 revolution after X/A alignment
const long  HOME_BACKOFF_STEPS = (long)(HOME_BACKOFF_TURNS * STEPS_PER_REVOLUTION);
const float HOME_Y_BACKOFF_TURNS = 0.25;   // Y back-off after hitting limit (quarter turn)
const long  HOME_Y_BACKOFF_STEPS = (long)(HOME_Y_BACKOFF_TURNS * STEPS_PER_REVOLUTION);
const unsigned long HOME_TIMEOUT_MS = 5000;  // Max wait for second motor (5s)

// HOME_PHASE_TIMEOUT_MS: hard cap on each individual homing phase. If a
// motor is driving toward a limit and that limit hasn't fired within this
// window, we assume the motor has lost traction (slipped belt, jammed
// gantry, mis-wired switch) and abort the entire HOME with a STUCK failure.
// Without this cap a slipping motor would just spin forever — burning the
// driver and giving the operator no feedback. 20 s is comfortably longer
// than a healthy home run (~4 s for X, ~3 s for Y on this gantry) but
// short enough to react before damage.
const unsigned long HOME_PHASE_TIMEOUT_MS = 20000;

// Limit-switch debounce: a switch must read LOW for this many consecutive
// reads (one per loop iteration) before we treat it as confirmed-hit. This
// prevents transient noise on the limit lines from being missed or causing
// flicker. With a typical loop rate of ~1 kHz, 3 reads is ~3 ms — fast enough
// that we still stop well before the gantry crashes anything, but robust
// against single-sample glitches.
const uint8_t LIMIT_DEBOUNCE_READS = 3;

// Claw servo angles (absolute 0-180 range, but clamped to physical limits below)
// PHYSICAL LIMITS — any requested angle is clamped to [SERVO_MIN_ANGLE, SERVO_MAX_ANGLE]
// to protect the servo from attempting impossible positions.
const int SERVO_MIN_ANGLE    = 23;   // Physical limit: fully closed
const int SERVO_MAX_ANGLE    = 90;   // Physical limit: fully open
const int SERVO_OPEN_ANGLE   = 90;   // Default "open" angle (at max limit)
const int SERVO_CLOSED_ANGLE = 23;   // Default "closed" angle (at min limit)
const int SERVO_START_ANGLE  = 90;   // Boot angle (open)
const int SERVO_HOME_ANGLE   = 90;   // Open position after homing

// Stepper speed/acceleration
const float MAX_SPEED     = 600;   // steps/sec
const float ACCELERATION  = 300;    // steps/sec^2
const float Z_MAX_SPEED   = 400;  // was 600
const float Z_ACCELERATION = 250;  // was 300

// Motor idle timeout — very long so motors stay energized and the claw does not
// fall under gravity. If you want auto-disable back, lower this value.
const unsigned long MOTOR_IDLE_TIMEOUT_MS = 10000000;  // ~10000 seconds

// === OBJECTS ===

AccelStepper stepperX(AccelStepper::DRIVER, X_STP, X_DIR);
AccelStepper stepperA(AccelStepper::DRIVER, A_STP, A_DIR);
AccelStepper stepperY(AccelStepper::DRIVER, Y_STP, Y_DIR);
AccelStepper stepperZ(AccelStepper::DRIVER, Z_STP, Z_DIR);
Servo clawServo;

// === STATE MACHINE ===

enum State {
  IDLE,
  MOVING_X,
  BACKING_OFF_X,
  MOVING_Y,
  BACKING_OFF_Y,
  MOVING_Z,
  BACKING_OFF_Z,         // Z plain-move limit-hit backoff
  HOMING_Z_TO_LIMIT,
  HOMING_Z_BACKOFF,      // post-home Z backoff (HOME / ZHOME / RAISE)
  HOMING_TO_LIMIT,
  HOMING_WAIT,
  HOMING_BACKOFF,
  HOMING_Y_TO_LIMIT,
  HOMING_Y_BACKOFF,
  HOMING_OPEN_CLAW
};

State currentState = IDLE;

// Motor power management
bool motorsEnabled = false;
unsigned long lastMovementTime = 0;

// Servo
int currentServoAngle = SERVO_START_ANGLE;
int targetServoAngle  = SERVO_START_ANGLE;

// Homing / alignment tracking
bool xHitLimit = false;
bool aHitLimit = false;
unsigned long homeWaitStart = 0;

// Stamp for when the current phase's motor drive started. Used to enforce
// HOME_PHASE_TIMEOUT_MS — if no limit fires within this window, abort the
// HOME command with a STUCK failure.
unsigned long homePhaseStart = 0;

// Y homing tracking — set when the Y limit fires during HOMING_Y_TO_LIMIT.
bool yHomeHit = false;

// Limit-switch debounce counters. Each counter increments on every loop tick
// the switch reads LOW; a "confirmed hit" requires reaching LIMIT_DEBOUNCE_READS.
// Resetting to 0 on a HIGH read means transient noise can't accumulate.
uint8_t xLimitDebounce = 0;
uint8_t aLimitDebounce = 0;
uint8_t yLimitDebounce = 0;
uint8_t zTopLimitDebounce = 0;

// Z top-limit-switch tracking. zPositionKnown becomes true once the Z top
// limit has fired at least once this power cycle (or was held LOW at boot
// because the claw started at the top). Until then, all reported Z values
// are stale guesses — Z motion is allowed but the agent should home Z first
// to establish a real reference.
bool zPositionKnown = false;

// X-axis direction tracking — remembers the sign of the active commanded move
// so that when a limit switch fires (and zeroes out distanceToGo), we still
// know which way to back off.
int xMoveDirection = 0;

// Y-axis direction tracking — same pattern as X.
int yMoveDirection = 0;

// Z-axis direction tracking. +1 = downward (paying out cable), -1 = upward
// (retracting, motor frame is opposite due to Z_DOWN_SIGN). The top-limit
// switch is only polled during upward moves: a LOW reading on a downward
// move is ignored as noise (mechanically nonsensical — claw moving away
// from top).
int zMoveDirection = 0;

// Z absolute position tracker, in AGENT-FRAME steps (positive = down, 0 = top).
// At boot the value is meaningless until the Z top limit switch has fired
// (zPositionKnown becomes true). The very first upward Z move that hits
// the switch will resync this to 0; subsequent upward moves that hit the
// switch will also resync (correcting any cumulative step-skip drift).
long zPositionSteps = 0;

// Motor-frame position that corresponds to agent-frame Z=0. Updated whenever
// we re-zero the agent frame (limit switch, or boot). This lets us compute
// the current agent-frame Z position at any moment, including after a halt:
//   agent_pos = (motor_pos - zMotorOrigin) * Z_DOWN_SIGN
long zMotorOrigin = 0;

// While a Z move is in flight, we record the intended target in agent-frame
// steps so that runZAxis() can update zPositionSteps correctly when the move
// completes.
long zPendingTarget = 0;

// === ACTIVE COMMAND TRACKING ===
// Every in-flight motion command has an ID and a command letter. These are
// echoed in the DONE/ERR line when the command completes so the Python layer
// can correlate responses with its request queue.

String activeCmdId = "-";
char activeCmdLetter = '?';
bool haltRequested = false;
String pendingHaltId = "-";

// === LINE BUFFER for serial input ===

char lineBuf[CMD_BUFFER_SIZE];
uint8_t lineLen = 0;

// === HELPER FUNCTIONS ===

// Clamp angle to the physical servo range. Returns the clamped value.
// If clamping occurred, emits an EVT SERVO_CLAMPED event.
int clampServoAngle(int requested) {
  int clamped = requested;
  if (clamped < SERVO_MIN_ANGLE) clamped = SERVO_MIN_ANGLE;
  if (clamped > SERVO_MAX_ANGLE) clamped = SERVO_MAX_ANGLE;
  if (clamped != requested) {
    Serial.print(F("EVT SERVO_CLAMPED requested="));
    Serial.print(requested);
    Serial.print(F(" actual="));
    Serial.println(clamped);
  }
  return clamped;
}

// Clamp a desired Z target step count to the absolute travel range.
// Returns the clamped target. If clamping occurred, emits EVT Z_CLAMPED.
// Clamp a desired Z target step count to the hard travel bounds.
//   Top    (Z = 0):           hard, no retraction above this.
//   Bottom (Z = Z_MAX_STEPS):  hard SAFETY bound — protects the spool from
//                              fully unwinding and reverse-winding.
// Returns the clamped target. If clamping happened, emits EVT Z_CLAMPED with
// a reason code so the agent can narrate intelligently.
long clampZTarget(long requested_target_steps) {
  long clamped = requested_target_steps;
  const char* reason = "";
  if (clamped < Z_MIN_STEPS) {
    clamped = Z_MIN_STEPS;
    reason = "top_bound";
  } else if (clamped > Z_MAX_STEPS) {
    clamped = Z_MAX_STEPS;
    reason = "bottom_bound";
  }
  if (clamped != requested_target_steps) {
    Serial.print(F("EVT Z_CLAMPED requested_steps="));
    Serial.print(requested_target_steps);
    Serial.print(F(" actual_steps="));
    Serial.print(clamped);
    Serial.print(F(" reason="));
    Serial.println(reason);
  }
  return clamped;
}

// Convert AccelStepper's current motor-frame position into agent-frame steps.
// Used for live position queries (STATE?) and for resyncing after a halt.
long currentZAgentPosition() {
  return (stepperZ.currentPosition() - zMotorOrigin) * Z_DOWN_SIGN;
}

void setServoTarget(int angle) {
  int safe = clampServoAngle(angle);
  targetServoAngle = safe;
  Serial.print(F("EVT SERVO_TARGET angle="));
  Serial.println(targetServoAngle);
}

void updateServo() {
  // Gradually move servo toward target (called each loop, rate-limited)
  if (currentServoAngle != targetServoAngle) {
    if (currentServoAngle < targetServoAngle) {
      currentServoAngle++;
    } else {
      currentServoAngle--;
    }
    clawServo.write(currentServoAngle);
  }
}

void enableMotors() {
  if (!motorsEnabled) {
    digitalWrite(EN, LOW);
    motorsEnabled = true;
    delay(10);
    Serial.println(F("EVT MOTORS ENABLED"));
  }
}

void disableMotors() {
  if (motorsEnabled) {
    digitalWrite(EN, HIGH);
    motorsEnabled = false;
    Serial.println(F("EVT MOTORS DISABLED"));
  }
}

// Hard-stop a stepper: zero out its remaining travel immediately
void hardStop(AccelStepper &stepper) {
  stepper.setCurrentPosition(stepper.currentPosition());
}

// Abort the current HOME sequence due to a phase timeout (motor presumed
// stuck — slipping belt, jammed gantry, slipping cable spool, or mis-wired
// limit switch). Stops all motion, clears homing flags, returns to IDLE,
// and emits DONE HOME STUCK so the agent can surface the failure.
void abortHomingStuck(const __FlashStringHelper *axisLabel) {
  hardStop(stepperX);
  hardStop(stepperA);
  hardStop(stepperY);
  hardStop(stepperZ);
  Serial.print(F("EVT HOME_STUCK axis="));
  Serial.println(axisLabel);
  xHitLimit = false;
  aHitLimit = false;
  yHomeHit = false;
  xLimitDebounce = 0;
  aLimitDebounce = 0;
  yLimitDebounce = 0;
  zTopLimitDebounce = 0;
  zMoveDirection = 0;
  // Use the active command letter to pick the right DONE label. ZHOME
  // (letter 'R') stuck completes as "ZHOME STUCK"; full HOME (letter
  // 'H') completes as "HOME STUCK".
  if (activeCmdLetter == 'R') {
    emitDone("ZHOME", "STUCK");
  } else {
    emitDone("HOME", "STUCK");
  }
  lastMovementTime = millis();
  currentState = IDLE;
  activeCmdLetter = '?';
  activeCmdId = "-";
}

// Update a debounce counter from a fresh limit-switch read.
// Returns true once the counter has reached LIMIT_DEBOUNCE_READS, signalling
// a confirmed hit. The counter resets to 0 on any HIGH read.
//
// Why debounce on the homing path: switch contacts can bounce on impact,
// producing brief HIGH transitions in the middle of an actual hit. Without
// debouncing the firmware could see one LOW (fire hardStop), then on the
// next loop iteration see HIGH (decide everything is fine), and keep
// dispatching. With debouncing, the LOW reading must persist for several
// reads before we act, AND a brief HIGH after that doesn't undo the hit
// flag (which is sticky for the duration of the homing sequence).
bool debouncedLimitHit(int pin, uint8_t &counter) {
  if (digitalRead(pin) == LOW) {
    if (counter < 255) counter++;
    return counter >= LIMIT_DEBOUNCE_READS;
  }
  counter = 0;
  return false;
}

// Force a stepper to a hard, sustained stop. Unlike hardStop() (single call),
// this is meant to be invoked every loop iteration on a motor that should
// stay stopped — defensive belt-and-suspenders against any AccelStepper
// edge case where setCurrentPosition alone isn't enough.
void clampStopped(AccelStepper &stepper) {
  long pos = stepper.currentPosition();
  stepper.setCurrentPosition(pos);  // resets target to current, zeroes speed
  stepper.moveTo(pos);              // explicit no-op target
}

// Emit ACK for the currently-activating command
void emitAck(const char* cmd) {
  Serial.print(F("ACK "));
  Serial.print(activeCmdId);
  Serial.print(F(" "));
  Serial.println(cmd);
}

// Emit DONE with status for the active command
void emitDone(const char* cmd, const char* status) {
  Serial.print(F("DONE "));
  Serial.print(activeCmdId);
  Serial.print(F(" "));
  Serial.print(cmd);
  Serial.print(F(" "));
  Serial.println(status);
}

// Emit ERR for a rejected command using a specific (id, reason)
void emitErr(const String& id, const char* reason) {
  Serial.print(F("ERR "));
  Serial.print(id);
  Serial.print(F(" "));
  Serial.println(reason);
}

// === SETUP ===

void setup() {
  // Start with motors enabled so the claw holds position from boot
  pinMode(EN, OUTPUT);
  digitalWrite(EN, LOW);
  motorsEnabled = true;

  // Configure limit switches with internal pullups
  pinMode(X_LIMIT, INPUT_PULLUP);
  pinMode(A_LIMIT, INPUT_PULLUP);
  pinMode(Y_LIMIT, INPUT_PULLUP);
  pinMode(Z_TOP_LIMIT, INPUT_PULLUP);

  // Configure steppers
  stepperX.setMaxSpeed(MAX_SPEED);
  stepperX.setAcceleration(ACCELERATION);

  stepperA.setMaxSpeed(MAX_SPEED);
  stepperA.setAcceleration(ACCELERATION);

  stepperY.setMaxSpeed(MAX_SPEED);
  stepperY.setAcceleration(ACCELERATION);

  stepperZ.setMaxSpeed(Z_MAX_SPEED);
  stepperZ.setAcceleration(Z_ACCELERATION);

  // Configure servo on A3
  clawServo.attach(CLAW_SERVO_PIN);
  currentServoAngle = SERVO_START_ANGLE;
  targetServoAngle  = SERVO_START_ANGLE;
  clawServo.write(currentServoAngle);

  Serial.begin(SERIAL_BAUD);
  delay(50);
  Serial.println(F("READY"));
  Serial.print(F("INFO firmware=claw_V4_agent servo_range="));
  Serial.print(SERVO_MIN_ANGLE);
  Serial.print(F("-"));
  Serial.println(SERVO_MAX_ANGLE);
  Serial.print(F("INFO z_max_steps="));
  Serial.print(Z_MAX_STEPS);
  Serial.print(F(" z_max_down_degrees="));
  Serial.print(Z_MAX_DOWN_DEGREES);
  Serial.print(F(" z_reference_steps="));
  Serial.print(Z_REFERENCE_DOWN_STEPS);
  Serial.print(F(" z_down_sign="));
  Serial.println(Z_DOWN_SIGN);

  // Check Z top limit at boot. If the claw happens to be at the top
  // already (switch reading LOW), we can establish Z = 0 immediately
  // without requiring a ZHOME command. Otherwise zPositionKnown stays
  // false and the agent will guide the user to ZHOME first.
  if (digitalRead(Z_TOP_LIMIT) == LOW) {
    zPositionKnown = true;
    zPositionSteps = 0;
    zPendingTarget = 0;
    zMotorOrigin = stepperZ.currentPosition();
    Serial.println(F("INFO z_position=KNOWN (top switch held at boot)"));
  } else {
    Serial.println(F("INFO z_position=UNKNOWN — send ZHOME to establish reference"));
  }
}

// === MAIN LOOP ===

void loop() {
  // Update servo position (slow sweep, ~100 deg/sec max)
  static unsigned long lastServoUpdate = 0;
  if (millis() - lastServoUpdate >= 10) {
    updateServo();
    lastServoUpdate = millis();
  }

  // Serial is polled EVERY loop iteration (not just when IDLE) so that HALT,
  // STATE?, and PING work mid-motion. Other commands are rejected with
  // ERR ... BUSY when issued during an active motion.
  pollSerial();

  // If a HALT was requested, service it now by stopping all motion and
  // completing the active command with status HALTED.
  if (haltRequested) {
    serviceHalt();
  }

  // Auto-disable motors after idle timeout (effectively disabled by long timeout).
  if (currentState == IDLE && motorsEnabled) {
    if (millis() - lastMovementTime >= MOTOR_IDLE_TIMEOUT_MS) {
      disableMotors();
    }
  }

  // Run state machine
  switch (currentState) {
    case IDLE:              break;
    case MOVING_X:          runXAxis();           break;
    case BACKING_OFF_X:     runXBackoff();        break;
    case MOVING_Y:          runYAxis();           break;
    case BACKING_OFF_Y:     runYBackoff();        break;
    case MOVING_Z:          runZAxis();           break;
    case BACKING_OFF_Z:     runZBackoff();        break;
    case HOMING_Z_TO_LIMIT: runHomingZToLimit();  break;
    case HOMING_Z_BACKOFF:  runHomingZBackoff();  break;
    case HOMING_TO_LIMIT:   runHomingToLimit();   break;
    case HOMING_WAIT:       runHomingWait();      break;
    case HOMING_BACKOFF:    runHomingBackoff();   break;
    case HOMING_Y_TO_LIMIT: runHomingYToLimit();  break;
    case HOMING_Y_BACKOFF:  runHomingYBackoff();  break;
    case HOMING_OPEN_CLAW:  runHomingOpenClaw();  break;
  }
}

// === SERIAL POLLING ===
//
// Read characters into a line buffer until we see '\n'. When a full line is
// received, hand it to dispatchLine().

void pollSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineLen > 0) {
        dispatchLine(lineBuf);
      }
      lineLen = 0;
    } else if (lineLen < CMD_BUFFER_SIZE - 1) {
      lineBuf[lineLen++] = c;
    } else {
      // Overflow — reset buffer
      lineLen = 0;
      emitErr("-", "BAD_ARGS");
    }
  }
}

// === LINE DISPATCH ===
//
// Parses a complete command line of the form:
//   [#<id> ] <CMD> [<args>]
// Any of HALT, STATE?, PING are allowed even during active motion.
// All other commands during motion return ERR <id> BUSY.

void dispatchLine(char* line) {
  // Skip leading whitespace
  char* p = line;
  while (*p == ' ' || *p == '\t') p++;

  // Optional command ID: "#<token>"
  String cmdId = "-";
  if (*p == '#') {
    p++;
    char* idStart = p;
    while (*p && *p != ' ' && *p != '\t') p++;
    char save = *p;
    *p = '\0';
    cmdId = String(idStart);
    *p = save;
    while (*p == ' ' || *p == '\t') p++;
  }

  if (*p == '\0') {
    emitErr(cmdId, "BAD_ARGS");
    return;
  }

  // Upper-case the command keyword (first token) in place
  char* kw = p;
  while (*p && *p != ' ' && *p != '\t') {
    *p = toupper(*p);
    p++;
  }
  while (*p == ' ' || *p == '\t') {
    *p = '\0';
    p++;
  }
  // p now points to the argument portion (possibly empty)

  // --- Commands allowed at ANY time ---

  if (strcmp(kw, "HALT") == 0) {
    // Schedule halt; actual stop work happens in serviceHalt() next loop tick.
    // Echo ACK immediately with the halt's own id.
    Serial.print(F("ACK "));
    Serial.print(cmdId);
    Serial.println(F(" HALT"));
    haltRequested = true;
    // Remember the halt's id so serviceHalt() can DONE it.
    pendingHaltId = cmdId;
    return;
  }

  if (strcmp(kw, "STATE?") == 0 || strcmp(kw, "STATE") == 0) {
    emitStateLine(cmdId);
    return;
  }

  if (strcmp(kw, "PING") == 0) {
    Serial.print(F("PONG "));
    Serial.println(cmdId);
    return;
  }

  // --- Commands requiring the firmware to be NOT mid-motion ---
  //
  // Any state other than IDLE means a motion is in flight.
  if (currentState != IDLE) {
    emitErr(cmdId, "BUSY");
    return;
  }

  // Commit this command as the active one
  activeCmdId = cmdId;

  if (strcmp(kw, "EN") == 0) {
    activeCmdLetter = 'E';
    emitAck("EN");
    enableMotors();
    emitDone("EN", "OK");
    return;
  }

  if (strcmp(kw, "DIS") == 0) {
    activeCmdLetter = 'D';
    emitAck("DIS");
    disableMotors();
    emitDone("DIS", "OK");
    return;
  }

  // ZHOME / RAISE: drive Z upward until the top limit switch fires. On hit
  // we resync zPositionSteps = 0, set zPositionKnown = true, then back off
  // by Z_HOME_BACKOFF_DEGREES so the claw isn't pressed against the
  // (delicate) switch. The two command names are aliases — ZHOME is the
  // explicit name, RAISE is kept for readability ("raise the claw to the
  // top") and backwards compat.
  if (strcmp(kw, "ZHOME") == 0 || strcmp(kw, "RAISE") == 0) {
    activeCmdLetter = 'R';
    enableMotors();
    emitAck(kw);
    // Reset the top-limit debounce. If the switch is already pressed at
    // the start of this command, the per-tick check in runHomingZToLimit
    // will catch it within LIMIT_DEBOUNCE_READS ticks and resync without
    // any actual motion (then back off the small amount).
    zTopLimitDebounce = 0;
    zMoveDirection = -1;  // upward
    // Drive a large negative-agent (= -Z_DOWN_SIGN motor) move; we expect
    // the limit switch to terminate it well before the target is reached.
    long upDistance = (long)(360.0 * 200.0 * STEPS_PER_DEGREE);  // 200 turns
    stepperZ.move(-Z_DOWN_SIGN * upDistance);
    homePhaseStart = millis();
    currentState = HOMING_Z_TO_LIMIT;
    return;
  }

  if (strcmp(kw, "HOME") == 0) {
    // Full homing sequence — Z first (so the claw is retracted and won't
    // swing into anything during X/Y motion), then X+A, then Y, then claw
    // open. On completion, Z is resynced (and backed off slightly) and the
    // gantry is parked at the front-left dropoff corner.
    activeCmdLetter = 'H';
    emitAck("HOME");
    enableMotors();
    xHitLimit = false;
    aHitLimit = false;
    yHomeHit = false;
    xLimitDebounce = 0;
    aLimitDebounce = 0;
    yLimitDebounce = 0;
    zTopLimitDebounce = 0;
    // Phase 0: drive Z up. After it hits, runHomingZToLimit transitions
    // into HOMING_Z_BACKOFF for the small post-home backoff, and from
    // there into HOMING_TO_LIMIT for the X+A phase. activeCmdLetter
    // stays 'H' so the eventual emitDone uses "HOME".
    zMoveDirection = -1;
    long upDistance = (long)(360.0 * 200.0 * STEPS_PER_DEGREE);
    stepperZ.move(-Z_DOWN_SIGN * upDistance);
    homePhaseStart = millis();
    currentState = HOMING_Z_TO_LIMIT;
    return;
  }

  if (strcmp(kw, "OPEN") == 0) {
    activeCmdLetter = 'O';
    int angle = SERVO_OPEN_ANGLE;
    if (*p != '\0') angle = atoi(p);
    emitAck("OPEN");
    setServoTarget(angle);
    emitDone("OPEN", "OK");
    return;
  }

  if (strcmp(kw, "CLOSE") == 0) {
    activeCmdLetter = 'C';
    int angle = SERVO_CLOSED_ANGLE;
    if (*p != '\0') angle = atoi(p);
    emitAck("CLOSE");
    setServoTarget(angle);
    emitDone("CLOSE", "OK");
    return;
  }

  if (strcmp(kw, "S") == 0 || strcmp(kw, "SERVO") == 0) {
    activeCmdLetter = 'S';
    if (*p == '\0') {
      emitErr(cmdId, "BAD_ARGS");
      return;
    }
    int angle = atoi(p);
    emitAck("S");
    setServoTarget(angle);
    emitDone("S", "OK");
    return;
  }

  if (strcmp(kw, "X") == 0 || strcmp(kw, "Y") == 0 || strcmp(kw, "Z") == 0) {
    if (*p == '\0') {
      emitErr(cmdId, "BAD_ARGS");
      return;
    }
    float degrees = atof(p);
    if (degrees == 0.0) {
      // Zero-degree move — complete immediately without any motor activity
      activeCmdLetter = kw[0];
      char cmdBuf[2] = { kw[0], '\0' };
      emitAck(cmdBuf);
      emitDone(cmdBuf, "OK");
      return;
    }

    enableMotors();
    long steps = (long)(degrees * STEPS_PER_DEGREE);
    activeCmdLetter = kw[0];
    char cmdBuf[2] = { kw[0], '\0' };
    emitAck(cmdBuf);

    if (kw[0] == 'X') {
      Serial.print(F("EVT MOVING axis=X degrees="));
      Serial.print(degrees);
      Serial.print(F(" steps="));
      Serial.println(steps);
      xMoveDirection = (steps > 0) ? 1 : -1;
      stepperX.move(steps);
      stepperA.move(steps);
      currentState = MOVING_X;
    } else if (kw[0] == 'Y') {
      Serial.print(F("EVT MOVING axis=Y degrees="));
      Serial.print(degrees);
      Serial.print(F(" steps="));
      Serial.println(steps);
      yMoveDirection = (steps > 0) ? 1 : -1;
      stepperY.move(steps);
      currentState = MOVING_Y;
    } else if (kw[0] == 'Z') {
      // Z move: positive degrees = down. Software clamps target to
      // [0, Z_MAX_STEPS]. The HARDWARE top limit switch will also stop
      // upward motion (resyncing Z=0 and backing off) before we ever
      // hit the software top bound — the software clamp is a backstop
      // in case the switch fails or isn't yet wired.
      long requested_target = zPositionSteps + steps;
      long clamped_target = clampZTarget(requested_target);
      long delta_agent = clamped_target - zPositionSteps;
      long delta_motor = Z_DOWN_SIGN * delta_agent;
      zPendingTarget = clamped_target;
      zMoveDirection = (delta_agent > 0) ? +1 : (delta_agent < 0 ? -1 : 0);

      Serial.print(F("EVT MOVING axis=Z degrees="));
      Serial.print(degrees);
      Serial.print(F(" delta_agent="));
      Serial.print(delta_agent);
      Serial.print(F(" delta_motor="));
      Serial.print(delta_motor);
      Serial.print(F(" from="));
      Serial.print(zPositionSteps);
      Serial.print(F(" to="));
      Serial.println(clamped_target);

      if (delta_agent == 0) {
        emitDone("Z", "OK");
        return;
      }
      // Reset top-limit debounce so we get a clean read on this move.
      // (If the switch is currently held LOW because the claw is at the
      // top, an upward move will trigger the limit handler immediately
      // and resync; a downward move is the way out and the handler
      // doesn't fire on downward moves.)
      zTopLimitDebounce = 0;
      stepperZ.move(delta_motor);
      currentState = MOVING_Z;
    }
    return;
  }

  // Unknown command
  emitErr(cmdId, "UNKNOWN");
}

// === HALT SERVICE ===
//
// Halt plumbing: the dispatcher records the halting command's id in
// pendingHaltId so serviceHalt() can emit both a DONE for the ACTIVE motion
// command AND a DONE for the HALT itself.

void serviceHalt() {
  haltRequested = false;

  // If there's a motion in progress, stop everything and complete the active
  // command with status HALTED. This includes aborting any HOME sub-state.
  if (currentState != IDLE) {
    hardStop(stepperX);
    hardStop(stepperA);
    hardStop(stepperY);
    hardStop(stepperZ);

    // If Z motion was halted mid-flight, resync the agent-frame Z position
    // from where the motor actually stopped. Without this, zPositionSteps
    // would still hold the pre-move value (or the not-yet-reached target),
    // and subsequent Z bounds checks would be off by however far the motor
    // got before the halt.
    if (currentState == MOVING_Z ||
        currentState == BACKING_OFF_Z ||
        currentState == HOMING_Z_TO_LIMIT ||
        currentState == HOMING_Z_BACKOFF) {
      zPositionSteps = currentZAgentPosition();
      zPendingTarget = zPositionSteps;
      Serial.print(F("EVT Z_RESYNC reason=halt z_pos="));
      Serial.println(zPositionSteps);
    }

    // Determine what command was active so we DONE it properly.
    char cmdBuf[8];
    switch (activeCmdLetter) {
      case 'X': strcpy(cmdBuf, "X"); break;
      case 'Y': strcpy(cmdBuf, "Y"); break;
      case 'Z': strcpy(cmdBuf, "Z"); break;
      case 'H': strcpy(cmdBuf, "HOME"); break;
      case 'R': strcpy(cmdBuf, "ZHOME"); break;
      default:  strcpy(cmdBuf, "?"); break;
    }
    emitDone(cmdBuf, "HALTED");

    currentState = IDLE;
    lastMovementTime = millis();
    activeCmdLetter = '?';
    activeCmdId = "-";

    // Clear all homing flags & debounce so a future HOME starts clean
    xHitLimit = false;
    aHitLimit = false;
    yHomeHit = false;
    xLimitDebounce = 0;
    aLimitDebounce = 0;
    yLimitDebounce = 0;
    zTopLimitDebounce = 0;
    xMoveDirection = 0;
    yMoveDirection = 0;
    zMoveDirection = 0;
  }

  // Complete the HALT command itself.
  Serial.print(F("DONE "));
  Serial.print(pendingHaltId);
  Serial.println(F(" HALT OK"));
  pendingHaltId = "-";
}

// === STATE REPORT ===
//
// Emits a single line of the form:
//   STATE <id> IDLE X=0 Y=0 Z=0 LX=0 LA=0 LY=0 SERVO=80 EN=1
// Values:
//   LX/LA/LZ are 1 when the limit switch is TRIGGERED (pin LOW), 0 otherwise.
//   EN is 1 when motor drivers are enabled, 0 otherwise.

void emitStateLine(const String& id) {
  const char* stateName = "UNKNOWN";
  switch (currentState) {
    case IDLE:              stateName = "IDLE"; break;
    case MOVING_X:          stateName = "MOVING_X"; break;
    case BACKING_OFF_X:     stateName = "BACKING_OFF_X"; break;
    case MOVING_Y:          stateName = "MOVING_Y"; break;
    case BACKING_OFF_Y:     stateName = "BACKING_OFF_Y"; break;
    case MOVING_Z:          stateName = "MOVING_Z"; break;
    case BACKING_OFF_Z:     stateName = "BACKING_OFF_Z"; break;
    case HOMING_Z_TO_LIMIT: stateName = "HOMING_Z_TO_LIMIT"; break;
    case HOMING_Z_BACKOFF:  stateName = "HOMING_Z_BACKOFF"; break;
    case HOMING_TO_LIMIT:   stateName = "HOMING_TO_LIMIT"; break;
    case HOMING_WAIT:       stateName = "HOMING_WAIT"; break;
    case HOMING_BACKOFF:    stateName = "HOMING_BACKOFF"; break;
    case HOMING_Y_TO_LIMIT: stateName = "HOMING_Y_TO_LIMIT"; break;
    case HOMING_Y_BACKOFF:  stateName = "HOMING_Y_BACKOFF"; break;
    case HOMING_OPEN_CLAW:  stateName = "HOMING_OPEN_CLAW"; break;
  }

  int lx = (digitalRead(X_LIMIT) == LOW) ? 1 : 0;
  int la = (digitalRead(A_LIMIT) == LOW) ? 1 : 0;
  int ly = (digitalRead(Y_LIMIT) == LOW) ? 1 : 0;
  int lz = (digitalRead(Z_TOP_LIMIT) == LOW) ? 1 : 0;

  Serial.print(F("STATE "));
  Serial.print(id);
  Serial.print(F(" "));
  Serial.print(stateName);
  Serial.print(F(" X="));
  Serial.print(stepperX.currentPosition());
  Serial.print(F(" Y="));
  Serial.print(stepperY.currentPosition());
  Serial.print(F(" Z="));
  Serial.print(currentZAgentPosition());
  Serial.print(F(" ZMAX="));
  Serial.print(Z_MAX_STEPS);
  Serial.print(F(" ZREF="));
  Serial.print(Z_REFERENCE_DOWN_STEPS);
  Serial.print(F(" ZHOMED="));
  Serial.print(zPositionKnown ? 1 : 0);
  Serial.print(F(" LX="));
  Serial.print(lx);
  Serial.print(F(" LA="));
  Serial.print(la);
  Serial.print(F(" LY="));
  Serial.print(ly);
  Serial.print(F(" LZTOP="));
  Serial.print(lz);
  Serial.print(F(" SERVO="));
  Serial.print(currentServoAngle);
  Serial.print(F(" EN="));
  Serial.println(motorsEnabled ? 1 : 0);
}

// === X-AXIS MOVEMENT (both motors together) ===

void runXAxis() {
  bool xMoving = stepperX.distanceToGo() != 0;
  bool aMoving = stepperA.distanceToGo() != 0;

  if (xMoving || aMoving) {
    // Use the same debounce path as homing for consistency. The counters
    // are persistent across loop iterations so single-sample noise can't
    // cause a spurious hit, but a sustained LOW (real impact) is caught
    // within ~3 ms of contact.
    bool xHit = debouncedLimitHit(X_LIMIT, xLimitDebounce);
    bool aHit = debouncedLimitHit(A_LIMIT, aLimitDebounce);

    if (xHit || aHit) {
      hardStop(stepperX);
      hardStop(stepperA);

      if (xHit) Serial.println(F("EVT LIMIT axis=X"));
      if (aHit) Serial.println(F("EVT LIMIT axis=A"));

      long backupSteps = -xMoveDirection * X_BACKUP_STEPS;
      // Reset debounce so the backoff drive doesn't immediately re-trigger.
      xLimitDebounce = 0;
      aLimitDebounce = 0;
      stepperX.move(backupSteps);
      stepperA.move(backupSteps);
      Serial.print(F("EVT BACKUP axis=X steps="));
      Serial.println(backupSteps);
      currentState = BACKING_OFF_X;
    } else {
      if (xMoving) stepperX.run();
      if (aMoving) stepperA.run();
    }
  } else {
    emitDone("X", "OK");
    lastMovementTime = millis();
    currentState = IDLE;
    activeCmdLetter = '?';
    activeCmdId = "-";
  }
}

void runXBackoff() {
  bool xMoving = stepperX.distanceToGo() != 0;
  bool aMoving = stepperA.distanceToGo() != 0;

  if (xMoving || aMoving) {
    if (xMoving) stepperX.run();
    if (aMoving) stepperA.run();
  } else {
    emitDone("X", "LIMIT");
    lastMovementTime = millis();
    currentState = IDLE;
    activeCmdLetter = '?';
    activeCmdId = "-";
  }
}

// === Y-AXIS MOVEMENT (with two limit switches OR'd to Y_LIMIT) ===
//
// Behavior matches X: a single signal pin captures both ends of travel; on
// contact we hardStop, back off in the opposite direction, and complete the
// command with status LIMIT.

void runYAxis() {
  if (stepperY.distanceToGo() != 0) {
    bool yHit = debouncedLimitHit(Y_LIMIT, yLimitDebounce);

    if (yHit) {
      hardStop(stepperY);
      Serial.println(F("EVT LIMIT axis=Y"));

      // Back off in the direction opposite to the commanded move.
      yLimitDebounce = 0;
      long backupSteps = -yMoveDirection * Y_BACKUP_STEPS;
      stepperY.move(backupSteps);
      Serial.print(F("EVT BACKUP axis=Y steps="));
      Serial.println(backupSteps);
      currentState = BACKING_OFF_Y;
    } else {
      stepperY.run();
    }
  } else {
    emitDone("Y", "OK");
    lastMovementTime = millis();
    currentState = IDLE;
    activeCmdLetter = '?';
    activeCmdId = "-";
  }
}

void runYBackoff() {
  if (stepperY.distanceToGo() != 0) {
    stepperY.run();
  } else {
    emitDone("Y", "LIMIT");
    lastMovementTime = millis();
    currentState = IDLE;
    activeCmdLetter = '?';
    activeCmdId = "-";
  }
}

// === Z-AXIS MOVEMENT (top limit switch + software bottom bound) ===
//
// Z motion is handled in two cases:
//   1. Downward (zMoveDirection == +1): just step until distanceToGo() == 0.
//      The software bottom clamp at dispatch time prevents over-extension.
//      The top limit switch is NOT polled here (mechanically nonsensical to
//      hit the top while moving away from it).
//   2. Upward (zMoveDirection == -1): poll the top limit switch every tick.
//      On confirmed hit, hard-stop, resync zPositionSteps = 0, mark
//      zPositionKnown = true, and back off by Z_BACKUP_STEPS so the claw
//      isn't resting on the (delicate) switch. This is also the
//      auto-correction for any cumulative step-skipping caused by cable
//      spool tension shocks.

void runZAxis() {
  // Poll the top limit switch only on upward motion.
  if (zMoveDirection < 0) {
    if (debouncedLimitHit(Z_TOP_LIMIT, zTopLimitDebounce)) {
      hardStop(stepperZ);
      // Resync agent-frame position. The motor's physical position no longer
      // matters — what matters is that we are mechanically at the top.
      zMotorOrigin = stepperZ.currentPosition();
      zPositionSteps = 0;
      zPendingTarget = 0;
      zPositionKnown = true;
      Serial.println(F("EVT Z_TOP_HIT resync_zero=1"));

      // Back off downward by Z_BACKUP_STEPS so the claw isn't pressed
      // against the switch. zPendingTarget is updated so runZBackoff()
      // can sync zPositionSteps to it on completion.
      zTopLimitDebounce = 0;
      zMoveDirection = +1;  // backoff is downward
      zPendingTarget = Z_BACKUP_STEPS;
      stepperZ.move(Z_DOWN_SIGN * Z_BACKUP_STEPS);
      Serial.print(F("EVT BACKUP axis=Z steps="));
      Serial.println(Z_BACKUP_STEPS);
      currentState = BACKING_OFF_Z;
      return;
    }
  }

  if (stepperZ.distanceToGo() != 0) {
    stepperZ.run();
  } else {
    // Move completed at the commanded target without hitting the top switch.
    zPositionSteps = zPendingTarget;
    emitDone("Z", "OK");
    lastMovementTime = millis();
    currentState = IDLE;
    activeCmdLetter = '?';
    activeCmdId = "-";
    zMoveDirection = 0;
  }
}

// Backoff after the Z top limit fires during a plain Z command. Drives the
// claw downward by Z_BACKUP_STEPS off the switch, then completes with
// LIMIT status (so the agent knows the destination wasn't reached because
// the limit intervened).
void runZBackoff() {
  if (stepperZ.distanceToGo() != 0) {
    stepperZ.run();
    return;
  }
  zPositionSteps = zPendingTarget;
  emitDone("Z", "LIMIT");
  lastMovementTime = millis();
  currentState = IDLE;
  activeCmdLetter = '?';
  activeCmdId = "-";
  zMoveDirection = 0;
}

// === HOMING / ALIGNMENT SEQUENCE ===
//
// Multi-phase sequence kicked off by the HOME command:
//
//   Phase 0 — runHomingZToLimit:
//     Z drives upward until the Z top limit switch fires. Resyncs Z=0
//     and sets zPositionKnown=true. Doing this BEFORE the X/Y phases
//     means the claw is retracted and won't swing into anything as the
//     gantry moves around. If Z never hits within HOME_PHASE_TIMEOUT_MS
//     the entire HOME aborts with STUCK.
//
//   Phase 0.5 — runHomingZBackoff:
//     Drops the claw by Z_HOME_BACKOFF_STEPS so it isn't pressed against
//     the (delicate) top limit switch during the rest of homing or after
//     the sequence completes. The Z=0 reference is still the switch
//     trigger point itself; the claw simply parks slightly below it.
//
//   Phase 1 — runHomingToLimit:
//     Both X and A drive in the +X direction toward their limit switches.
//     Each motor stops INDIVIDUALLY when its switch confirms a hit
//     (debounced — must read LOW for LIMIT_DEBOUNCE_READS consecutive ticks).
//     Once a motor has hit, it is clamped on every subsequent tick (defensive
//     belt-and-suspenders against any case where the single hardStop didn't
//     fully arrest motion). The other motor keeps driving until either its
//     own limit fires, both have hit, or HOME_TIMEOUT_MS elapses.
//
//   Phase 2 — runHomingWait:
//     Brief 200 ms settle pause before backing off, so the limit-switch
//     debounce counters can fully relax and any mechanical bounce damps out.
//
//   Phase 3 — runHomingBackoff:
//     X and A both move HOME_BACKOFF_STEPS in the -X direction together.
//
//   Phase 4 — runHomingYToLimit:
//     Y drives toward its limit (per HOME_Y_DIRECTION) until Y_LIMIT fires
//     (debounced). The Y limit pin is OR'd between both physical Y switches,
//     so any LOW on Y_LIMIT halts the motion.
//
//   Phase 5 — runHomingYBackoff:
//     Y moves HOME_Y_BACKOFF_STEPS in the opposite direction (a quarter turn).
//
//   Phase 6 — runHomingOpenClaw:
//     Servo opens to SERVO_HOME_ANGLE; sequence completes with DONE HOME OK.

// Phase 0 of full HOME, or the entire ZHOME / RAISE command standalone.
// Drive Z upward until the top limit switch fires (debounced). On hit:
//   - Resync zPositionSteps = 0 / zMotorOrigin = current motor pos
//   - Set zPositionKnown = true
//   - Transition into HOMING_Z_BACKOFF for the small post-home dropoff
// Phase-level timeout aborts the command with STUCK if the switch never
// fires within HOME_PHASE_TIMEOUT_MS.
void runHomingZToLimit() {
  if (millis() - homePhaseStart >= HOME_PHASE_TIMEOUT_MS) {
    abortHomingStuck(F("Z"));
    return;
  }

  if (debouncedLimitHit(Z_TOP_LIMIT, zTopLimitDebounce)) {
    hardStop(stepperZ);
    zMotorOrigin = stepperZ.currentPosition();
    zPositionSteps = 0;
    zPendingTarget = 0;
    zPositionKnown = true;
    Serial.println(F("EVT Z_TOP_HIT resync_zero=1"));

    // Issue the small post-home backoff so the claw doesn't rest on the
    // delicate switch. zPendingTarget tracks where we'll be parked in
    // agent-frame steps when the backoff completes.
    zTopLimitDebounce = 0;
    zMoveDirection = +1;  // downward
    zPendingTarget = Z_HOME_BACKOFF_STEPS;
    stepperZ.move(Z_DOWN_SIGN * Z_HOME_BACKOFF_STEPS);
    Serial.print(F("EVT HOME_Z_BACKING_OFF steps="));
    Serial.println(Z_HOME_BACKOFF_STEPS);
    currentState = HOMING_Z_BACKOFF;
    return;
  }

  stepperZ.run();
}

// Phase 0.5: small dropoff after the Z top limit hit, applied to all
// homing paths (HOME, ZHOME, RAISE). On completion, dispatches to either
// the X+A homing phase (full HOME) or completes the standalone command.
void runHomingZBackoff() {
  if (stepperZ.distanceToGo() != 0) {
    stepperZ.run();
    return;
  }

  // Backoff complete. zPositionSteps now reflects the parked position
  // (Z_HOME_BACKOFF_STEPS below the switch in agent frame).
  zPositionSteps = zPendingTarget;
  zMoveDirection = 0;

  if (activeCmdLetter == 'H') {
    // Continue full HOME sequence into X+A phase.
    Serial.println(F("EVT HOME_X_DRIVING"));
    long xHomeMove = (long)HOME_X_DIRECTION * HOME_STEPS;
    stepperX.move(xHomeMove);
    stepperA.move(xHomeMove);
    homePhaseStart = millis();
    currentState = HOMING_TO_LIMIT;
    return;
  }

  // Standalone ZHOME / RAISE — done.
  emitDone("ZHOME", "OK");
  lastMovementTime = millis();
  currentState = IDLE;
  activeCmdLetter = '?';
  activeCmdId = "-";
}

void runHomingToLimit() {
  // Phase-level timeout: if the X+A phase hasn't completed within
  // HOME_PHASE_TIMEOUT_MS we assume a motor is slipping/jammed and abort
  // the entire HOME sequence. Catches the failure mode where a motor spins
  // forever without ever tripping its switch.
  if (millis() - homePhaseStart >= HOME_PHASE_TIMEOUT_MS) {
    const __FlashStringHelper *which =
        (!xHitLimit && !aHitLimit) ? F("X+A") :
        (!xHitLimit) ? F("X") : F("A");
    abortHomingStuck(which);
    return;
  }

  // Per-tick limit reads, debounced.
  bool xConfirmedHit = debouncedLimitHit(X_LIMIT, xLimitDebounce);
  bool aConfirmedHit = debouncedLimitHit(A_LIMIT, aLimitDebounce);

  // Edge transitions: fire once per motor to log the hit.
  if (xConfirmedHit && !xHitLimit) {
    hardStop(stepperX);
    xHitLimit = true;
    Serial.println(F("EVT HOME_X_HIT"));
    if (!aHitLimit) homeWaitStart = millis();
  }
  if (aConfirmedHit && !aHitLimit) {
    hardStop(stepperA);
    aHitLimit = true;
    Serial.println(F("EVT HOME_A_HIT"));
    if (!xHitLimit) homeWaitStart = millis();
  }

  // Defensive: re-clamp any motor that has hit. This runs every tick a motor
  // is in the "should be stopped" state, so even if AccelStepper somehow had
  // a residual step queued, this overrides it. Cheap, safe.
  if (xHitLimit) clampStopped(stepperX);
  if (aHitLimit) clampStopped(stepperA);

  // Drive only motors that haven't hit yet.
  if (!xHitLimit) stepperX.run();
  if (!aHitLimit) stepperA.run();

  if (xHitLimit && aHitLimit) {
    Serial.println(F("EVT HOME_BOTH_HIT"));
    currentState = HOMING_WAIT;
    homeWaitStart = millis();
  } else if (xHitLimit || aHitLimit) {
    if (millis() - homeWaitStart >= HOME_TIMEOUT_MS) {
      Serial.println(F("EVT HOME_TIMEOUT"));
      if (!xHitLimit) {
        hardStop(stepperX);
        xHitLimit = true;  // mark as "stopped" so clampStopped engages next tick
        Serial.println(F("EVT HOME_X_TIMEOUT"));
      }
      if (!aHitLimit) {
        hardStop(stepperA);
        aHitLimit = true;
        Serial.println(F("EVT HOME_A_TIMEOUT"));
      }
      currentState = HOMING_WAIT;
      homeWaitStart = millis();
    }
  }
}

void runHomingWait() {
  // Defensive: motors in this state are supposed to be stopped. Clamp them
  // every tick until we actually start the backoff move.
  clampStopped(stepperX);
  clampStopped(stepperA);

  if (millis() - homeWaitStart >= 200) {
    Serial.println(F("EVT HOME_BACKING_OFF"));
    // Reset debounce counters so a backoff drive doesn't immediately retrigger
    // the just-pressed switch.
    xLimitDebounce = 0;
    aLimitDebounce = 0;
    // Back off in the direction OPPOSITE the homing drive.
    long backoff = -(long)HOME_X_DIRECTION * HOME_BACKOFF_STEPS;
    stepperX.move(backoff);
    stepperA.move(backoff);
    currentState = HOMING_BACKOFF;
  }
}

void runHomingBackoff() {
  bool xMoving = stepperX.distanceToGo() != 0;
  bool aMoving = stepperA.distanceToGo() != 0;

  if (xMoving || aMoving) {
    if (xMoving) stepperX.run();
    if (aMoving) stepperA.run();
  } else {
    // X and A are now backed off and parked. Begin Y homing phase.
    Serial.println(F("EVT HOME_Y_DRIVING"));
    yHomeHit = false;
    yLimitDebounce = 0;
    yMoveDirection = HOME_Y_DIRECTION;
    long yHomeMove = (long)HOME_Y_DIRECTION * HOME_STEPS;
    stepperY.move(yHomeMove);
    homePhaseStart = millis();
    currentState = HOMING_Y_TO_LIMIT;
  }
}

void runHomingYToLimit() {
  // Phase-level timeout — see comment in runHomingToLimit.
  if (millis() - homePhaseStart >= HOME_PHASE_TIMEOUT_MS) {
    abortHomingStuck(F("Y"));
    return;
  }

  bool yConfirmedHit = debouncedLimitHit(Y_LIMIT, yLimitDebounce);

  // Edge transition: log the hit and arrest motion the first time we confirm.
  if (yConfirmedHit && !yHomeHit) {
    hardStop(stepperY);
    yHomeHit = true;
    Serial.println(F("EVT HOME_Y_HIT"));
    // Transition to backoff immediately on the same tick — issue the
    // opposite-direction backoff move and switch state. Avoid clampStopped
    // here because it would overwrite the move target we just set.
    yLimitDebounce = 0;
    long yBackoff = -(long)HOME_Y_DIRECTION * HOME_Y_BACKOFF_STEPS;
    stepperY.move(yBackoff);
    Serial.print(F("EVT HOME_Y_BACKING_OFF steps="));
    Serial.println(yBackoff);
    currentState = HOMING_Y_BACKOFF;
    return;
  }

  // Not yet hit — keep driving toward the switch.
  stepperY.run();

  // Defensive: if we somehow ran out of commanded travel without hitting
  // (limit switch wiring problem, mechanical overshoot, etc.), bail out
  // gracefully by transitioning to backoff anyway. The operator can re-run
  // HOME if the result is wrong.
  if (stepperY.distanceToGo() == 0) {
    Serial.println(F("EVT HOME_Y_NO_HIT"));
    yHomeHit = true;
    yLimitDebounce = 0;
    long yBackoff = -(long)HOME_Y_DIRECTION * HOME_Y_BACKOFF_STEPS;
    stepperY.move(yBackoff);
    currentState = HOMING_Y_BACKOFF;
  }
}

void runHomingYBackoff() {
  if (stepperY.distanceToGo() != 0) {
    stepperY.run();
  } else {
    Serial.println(F("EVT HOME_OPENING_CLAW"));
    setServoTarget(SERVO_HOME_ANGLE);
    currentState = HOMING_OPEN_CLAW;
  }
}

void runHomingOpenClaw() {
  if (currentServoAngle == targetServoAngle) {
    emitDone("HOME", "OK");
    lastMovementTime = millis();
    currentState = IDLE;
    activeCmdLetter = '?';
    activeCmdId = "-";
  }
}
