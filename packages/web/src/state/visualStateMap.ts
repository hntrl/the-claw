import type { BackgroundMode, MachineState, SpriteMood } from "../types/display";

export type VisualState = {
  sprite: SpriteMood;
  backgroundMode: BackgroundMode;
  headline: string;
  subtitle?: string;
  showTranscript: boolean;
  intensity: number;
  accentColor: string;
};

export const visualStateMap: Record<MachineState, VisualState> = {
  attract: {
    sprite: "calm",
    backgroundMode: "idleGrid",
    headline: "VOICE ACTIVATED CLAW AGENT",
    subtitle: "Say: grab the blue prize",
    showTranscript: false,
    intensity: 0.25,
    accentColor: "#8ec8ff",
  },
  listening: {
    sprite: "listening",
    backgroundMode: "audioGrid",
    headline: "LISTENING...",
    subtitle: "Parsing human intent",
    showTranscript: true,
    intensity: 0.55,
    accentColor: "#8ec8ff",
  },
  transcribing: {
    sprite: "listening",
    backgroundMode: "audioGrid",
    headline: "TRANSCRIBING...",
    showTranscript: true,
    intensity: 0.6,
    accentColor: "#8ec8ff",
  },
  thinking: {
    sprite: "thinking",
    backgroundMode: "thinkingGraph",
    headline: "PLANNING MOVE...",
    subtitle: "Routing through the graph",
    showTranscript: true,
    intensity: 0.7,
    accentColor: "#8d94ff",
  },
  targeting: {
    sprite: "thinking",
    backgroundMode: "thinkingGraph",
    headline: "ACQUIRING TARGET...",
    showTranscript: true,
    intensity: 0.75,
    accentColor: "#8d94ff",
  },
  moving: {
    sprite: "suspicious",
    backgroundMode: "motionVectors",
    headline: "EXECUTING MOVE...",
    showTranscript: true,
    intensity: 0.85,
    accentColor: "#8ec8ff",
  },
  dropping: {
    sprite: "suspicious",
    backgroundMode: "motionVectors",
    headline: "DROPPING CLAW...",
    showTranscript: true,
    intensity: 0.9,
    accentColor: "#ffe78a",
  },
  success: {
    sprite: "excited",
    backgroundMode: "celebration",
    headline: "OBJECT ACQUIRED",
    subtitle: "We are so back.",
    showTranscript: true,
    intensity: 1,
    accentColor: "#ffe78a",
  },
  failure: {
    sprite: "nervous",
    backgroundMode: "glitch",
    headline: "TARGET ESCAPED",
    subtitle: "Recalibrating emotionally...",
    showTranscript: true,
    intensity: 0.8,
    accentColor: "#ff606d",
  },
  error: {
    sprite: "nervous",
    backgroundMode: "glitch",
    headline: "SYSTEM ERROR",
    subtitle: "Manual vibes required.",
    showTranscript: true,
    intensity: 1,
    accentColor: "#ff606d",
  },
};

export const personalityCopy: Partial<Record<MachineState, string[]>> = {
  attract: [
    "VOICE LINK OPEN",
    "READY TO ACQUIRE OBJECTS",
    "SAY THE WORD. I HAVE CLAWS.",
    "AGENT CLAW SYSTEM ONLINE",
  ],
  listening: ["I'M LISTENING...", "PARSING HUMAN INTENT..."],
  thinking: [
    "ROUTING THROUGH THE GRAPH...",
    "CALCULATING CLAW VIBES...",
    "PLANNING TRAJECTORY...",
  ],
  moving: ["LOCKED IN.", "EXECUTING MOVE."],
  success: ["OBJECT ACQUIRED!", "WE ARE SO BACK."],
  failure: [
    "TARGET ESCAPED.",
    "MINOR CLAW INCIDENT.",
    "RECALIBRATING EMOTIONALLY.",
  ],
};
