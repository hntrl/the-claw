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

export const machineStates: MachineState[] = [
  "attract",
  "listening",
  "transcribing",
  "thinking",
  "targeting",
  "moving",
  "dropping",
  "success",
  "failure",
  "error",
];

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
  | "love";

export const spriteMoods: SpriteMood[] = [
  "calm",
  "blink",
  "wink",
  "suspicious",
  "excited",
  "confused",
  "thinking",
  "nervous",
  "stressed",
  "disappointed",
  "surprised",
  "love",
];

export type BackgroundMode =
  | "idleGrid"
  | "audioGrid"
  | "thinkingGraph"
  | "motionVectors"
  | "celebration"
  | "glitch";

export type AgentStep =
  | "speech_to_text"
  | "intent_parse"
  | "target_select"
  | "motion_plan"
  | "claw_execute"
  | "result_evaluate";

export const agentSteps: AgentStep[] = [
  "speech_to_text",
  "intent_parse",
  "target_select",
  "motion_plan",
  "claw_execute",
  "result_evaluate",
];

export type StepStatus = "pending" | "active" | "complete" | "error";

export type EventEffect =
  | "voiceDetected"
  | "commandParsed"
  | "moveStarted"
  | "dropStarted"
  | "grabSuccess"
  | "grabFailure"
  | "error";

export type DisplayEvent =
  | {
      type: "state";
      state: MachineState;
    }
  | {
      type: "emotion";
      mood: SpriteMood;
      emotion?: string;
      speed?: number;
      volume?: number;
    }
  | {
      type: "emotion_clear";
    }
  | {
      type: "transcript";
      text: string;
      isFinal: boolean;
    }
  | {
      type: "agent_step";
      step: AgentStep;
      status: StepStatus;
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
      effect: EventEffect;
    };

export type DisplayStore = {
  machineState: MachineState;
  transcript: string;
  isTranscriptFinal: boolean;
  moodOverride?: SpriteMood;
  currentStep?: AgentStep;
  stepStatuses: Record<AgentStep, StepStatus>;
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
  activeEffect?: EventEffect;
  effectNonce: number;
  wsStatus: "disconnected" | "connecting" | "connected";
};
