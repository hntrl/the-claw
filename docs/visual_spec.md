# Interrupt Voice-Controlled Claw Machine TV Visual Spec

## 1. Project context

This display is for a product activation at **Interrupt**, an agent conference for LangChain. The installation is a **voice-controlled claw machine** with a **vertical TV screen** in the background. The TV should feel like the claw machine's animated agent brain: playful, reactive, retro, and clearly connected to agent orchestration.

The mascot is a pixel-art green parrot scientist wearing glasses and a lab coat. The bird should act as the machine's emotional operator, reacting to voice input, planning, claw movement, success, and failure.

## 2. Core creative direction

The visual language should feel like:

> a retro arcade cabinet possessed by an AI agent runtime

Primary references:
- CRT pixel art
- arcade terminal UI
- vaporwave blue/purple accents
- LangGraph-style agent step/routing visuals
- voice-reactive grid / waveform animations
- playful mascot-driven product activation

The TV should not feel like a generic dashboard. It should feel alive, emotional, and theatrical from across the conference floor.

## 3. Screen format

Target display:
- Vertical TV / portrait orientation
- Recommended app aspect ratio: `9:16`
- Recommended canvas: `1080x1920` or responsive equivalent
- Background: black / near-black
- Pixel-art assets should use `image-rendering: pixelated`

Suggested vertical layout:

```txt
┌─────────────────────────────┐
│ LANGCHAIN INTERRUPT         │
│ AGENT CLAW SYSTEM           │
│ VOICE: ACTIVE  CLAW: ONLINE │
│                             │
│      ambient grid/fx        │
│                             │
│         BIRD SPRITE         │
│     circular portrait frame │
│                             │
│  CURRENT STATE HEADLINE     │
│  "live user transcript..."  │
│                             │
│  speech → plan → claw       │
│  target/confidence/status   │
└─────────────────────────────┘
```

Recommended space allocation:
- Top 15%: event/brand/system identity
- Middle 55%: sprite, circular frame, ambient visuals
- Bottom 30%: transcript, state, agent stepper, diagnostics

## 4. Visual layer architecture

Implement the UI as layered components:

```tsx
<App>
  <DisplayRuntimeProvider>
    <VerticalTVFrame>
      <AmbientLayer />
      <AgentGraphLayer />
      <SpriteStage />
      <HUDOverlay />
      <EventEffectsLayer />
      <CRTOverlay />
    </VerticalTVFrame>
  </DisplayRuntimeProvider>
</App>
```

Layer responsibilities:

### `AmbientLayer`
Always-on animated background. Should be visually interesting but not compete with the sprite.

Modes:
- `idleGrid`
- `audioGrid`
- `thinkingGraph`
- `motionVectors`
- `celebration`
- `glitch`

Suggested visuals:
- FLAKE-style ambient grid motion
- faint moving particles
- vector field distortions
- scanlines
- subtle CRT noise
- voice-reactive waveform pulses

### `AgentGraphLayer`
Small LangGraph-inspired visualization, especially during thinking/tool-calling.

Example sequence:

```txt
speech_to_text → intent_parser → target_selector → motion_planner → claw_controller → result_evaluator
```

Animate packets moving between nodes. Pulse the active node. Keep it subtle and readable.

### `SpriteStage`
Hero mascot stage. Renders the bird sprite according to state.

Requirements:
- centered circular-ish framing
- no full-body wide stance
- shoulders-and-above portrait
- black background or transparent sprite composited over black
- strong pixel-art edges
- slight bob/scale animation
- mood swaps based on state

### `HUDOverlay`
Text and diagnostics:
- current headline
- live transcript
- parsed intent
- target label
- confidence
- agent stepper
- claw action

### `EventEffectsLayer`
One-shot visual bursts:
- command recognized
- move started
- drop started
- success
- failure
- error

Examples:
- expanding pixel rings
- screen shake
- pixel confetti
- scanline flash
- red glitch ripple

### `CRTOverlay`
Global finishing layer:
- scanlines
- very subtle screen vignette
- low-opacity pixel/noise texture
- optional chromatic aberration, used sparingly

## 5. State model

The display should be driven by a simple state machine. The TV frontend should not infer agent behavior from raw internals; the backend/agent orchestration should emit display events.

```ts
export type MachineState =
  | "attract"
  | "listening"
  | "transcribing"
  | "thinking"
  | "targeting"
  | "moving"
  | "dropping"
  | "success"
  | "failure"
  | "error";
```

Suggested visual mapping:

```ts
export type SpriteMood =
  | "calm"
  | "blink"
  | "wink"
  | "suspicious"
  | "excited"
  | "confused"
  | "thinking"
  | "nervous"
  | "stressed"
  | "disappointed"
  | "surprised"
  | "love"
  | "idle"
  | "sleepy"
  | "inquisitive"
  | "lockedIn"
  | "celebratory";

export type BackgroundMode =
  | "idleGrid"
  | "audioGrid"
  | "thinkingGraph"
  | "motionVectors"
  | "celebration"
  | "glitch";
```

Recommended mapping:

```ts
export const visualStateMap = {
  attract: {
    sprite: "calm",
    backgroundMode: "idleGrid",
    headline: "VOICE ACTIVATED CLAW AGENT",
    subtitle: 'Say "grab the blue prize"',
    showTranscript: false,
    intensity: 0.25,
    accentColor: "purple",
  },
  listening: {
    sprite: "suspicious",
    backgroundMode: "audioGrid",
    headline: "LISTENING...",
    subtitle: "Parsing human intent",
    showTranscript: true,
    intensity: 0.55,
    accentColor: "cyan",
  },
  transcribing: {
    sprite: "confused",
    backgroundMode: "audioGrid",
    headline: "TRANSCRIBING...",
    showTranscript: true,
    intensity: 0.6,
    accentColor: "cyan",
  },
  thinking: {
    sprite: "thinking",
    backgroundMode: "thinkingGraph",
    headline: "PLANNING MOVE...",
    subtitle: "Routing through the graph",
    showTranscript: true,
    intensity: 0.7,
    accentColor: "blue",
  },
  targeting: {
    sprite: "suspicious",
    backgroundMode: "thinkingGraph",
    headline: "ACQUIRING TARGET...",
    intensity: 0.75,
    accentColor: "purple",
  },
  moving: {
    sprite: "stressed",
    backgroundMode: "motionVectors",
    headline: "EXECUTING MOVE...",
    intensity: 0.85,
    accentColor: "green",
  },
  dropping: {
    sprite: "nervous",
    backgroundMode: "motionVectors",
    headline: "DROPPING CLAW...",
    intensity: 0.9,
    accentColor: "yellow",
  },
  success: {
    sprite: "excited",
    backgroundMode: "celebration",
    headline: "OBJECT ACQUIRED",
    subtitle: "We are so back.",
    intensity: 1,
    accentColor: "yellow",
  },
  failure: {
    sprite: "disappointed",
    backgroundMode: "glitch",
    headline: "TARGET ESCAPED",
    subtitle: "Recalibrating emotionally...",
    intensity: 0.8,
    accentColor: "red",
  },
  error: {
    sprite: "stressed",
    backgroundMode: "glitch",
    headline: "SYSTEM ERROR",
    subtitle: "Manual vibes required.",
    intensity: 1,
    accentColor: "red",
  },
} as const;
```

## 6. Display event protocol

Use WebSocket or SSE from the claw/agent backend to the TV client.

```ts
export type AgentStep =
  | "speech_to_text"
  | "intent_parse"
  | "target_select"
  | "motion_plan"
  | "claw_execute"
  | "result_evaluate";

export type DisplayEvent =
  | {
      type: "state";
      state: MachineState;
    }
  | {
      type: "transcript";
      text: string;
      isFinal: boolean;
    }
  | {
      type: "agent_step";
      step: AgentStep;
      status: "pending" | "active" | "complete" | "error";
    }
  | {
      type: "claw_motion";
      direction: "left" | "right" | "forward" | "back" | "down" | "up";
      speed?: number;
    }
  | {
      type: "target";
      label: string;
      confidence?: number;
    }
  | {
      type: "result";
      outcome: "success" | "failure";
      label?: string;
    }
  | {
      type: "effect";
      effect:
        | "voiceDetected"
        | "commandParsed"
        | "moveStarted"
        | "dropStarted"
        | "grabSuccess"
        | "grabFailure"
        | "error";
    };
```

The display should maintain a small store:

```ts
export type DisplayStore = {
  machineState: MachineState;
  transcript: string;
  isTranscriptFinal: boolean;
  currentStep?: AgentStep;
  stepStatuses: Record<AgentStep, "pending" | "active" | "complete" | "error">;
  target?: {
    label: string;
    confidence?: number;
  };
  lastMotion?: {
    direction: string;
    speed?: number;
  };
  lastResult?: {
    outcome: "success" | "failure";
    label?: string;
  };
};
```

## 7. Frontend implementation recommendation

Recommended stack:
- React
- Vite
- TypeScript
- Zustand
- Framer Motion
- Canvas 2D or PixiJS
- WebSocket
- CSS modules or Tailwind
- `image-rendering: pixelated`

Avoid overbuilding the first version with Three.js unless someone already owns shader work. Canvas 2D is enough for a compelling grid, particles, waveform, scanlines, and event bursts.

## 8. Suggested MVP milestone

Build a local prototype with fake state controls before integrating the agent.

Keyboard controls:
- `1`: attract
- `2`: listening
- `3`: thinking
- `4`: moving
- `5`: dropping
- `6`: success
- `7`: failure
- `8`: error

MVP screen features:
- vertical black TV layout
- top system header
- animated grid background
- large bird sprite
- circular portrait framing
- live/fake transcript panel
- agent graph stepper
- state-based headline
- success/failure burst effects

## 9. Sprite asset notes

The included sprite assets are generated concept assets and should be treated as first-pass art direction. They are useful for prototyping the TV display, but final production may benefit from hand-cleaned sprite sheets.

Included moods:
- calm
- blink
- wink
- suspicious
- excited
- confused
- thinking
- nervous
- stressed
- disappointed
- surprised
- love

Recommended frontend usage:
- Use `sprites/transparent_no_labels/*.png` for the actual UI.
- Use `spritesheets/*.png` as reference sheets.
- Use `sprites/quadrants_with_labels/*.png` for design review.

CSS:

```css
.sprite {
  image-rendering: pixelated;
  image-rendering: crisp-edges;
  transform-origin: center;
}
```

## 10. Idle personality

The display should never feel dead. In attract mode, rotate micro-animations every few seconds:
- blink
- wink
- suspicious glance
- sleepy bob
- tiny scanline pulse
- terminal log flicker

Suggested idle copy:
- `VOICE LINK OPEN`
- `READY TO ACQUIRE OBJECTS`
- `SAY THE WORD. I HAVE CLAWS.`
- `AGENT CLAW SYSTEM ONLINE`

## 11. Voice/personality copy

Keep copy short, terminal-like, and playful.

Examples:

```ts
export const personalityCopy = {
  attract: [
    "VOICE LINK OPEN",
    "READY TO ACQUIRE OBJECTS",
    "SAY THE WORD. I HAVE CLAWS.",
  ],
  listening: [
    "I'M LISTENING...",
    "PARSING HUMAN INTENT...",
  ],
  thinking: [
    "ROUTING THROUGH THE GRAPH...",
    "CALCULATING CLAW VIBES...",
    "PLANNING TRAJECTORY...",
  ],
  moving: [
    "LOCKED IN.",
    "EXECUTING MOVE.",
  ],
  success: [
    "OBJECT ACQUIRED!",
    "WE ARE SO BACK.",
  ],
  failure: [
    "TARGET ESCAPED.",
    "MINOR CLAW INCIDENT.",
    "RECALIBRATING EMOTIONALLY.",
  ],
};
```

## 12. Definition of done for first coding-agent pass

A coding agent should be able to produce:

1. A Vite React app that fills a vertical TV screen.
2. A state-driven display runtime with keyboard debug controls.
3. Sprite rendering using the provided assets.
4. A Canvas background with animated grid/particles.
5. A HUD with state headline, transcript, target, confidence, and agent stepper.
6. Event effects for success/failure/drop/move.
7. A WebSocket event adapter using the `DisplayEvent` protocol above.
8. A simple local mock server or debug panel for testing events.

## 13. Implementation prompt for coding agent

Build a Vite + React + TypeScript app for a vertical TV display used in a voice-controlled claw machine product activation. The visual style is a retro CRT pixel-art agent terminal. Use the provided bird scientist sprite assets. The app should be driven by a `MachineState` state machine and a WebSocket event protocol. Implement layered visuals: animated ambient grid background, LangGraph-style agent step overlay, large centered sprite stage, HUD overlay, event effects, and CRT scanline overlay. Include keyboard debug controls to switch states locally. Use `image-rendering: pixelated` for sprites. Keep the app responsive for a 9:16 vertical display.
