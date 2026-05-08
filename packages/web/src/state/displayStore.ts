import { create } from "zustand";
import { agentSteps, type AgentStep, type DisplayEvent, type DisplayStore, type EventEffect, type MachineState } from "../types/display";

const initialStepStatuses = (): Record<AgentStep, "pending" | "active" | "complete" | "error"> => ({
  speech_to_text: "pending",
  intent_parse: "pending",
  target_select: "pending",
  motion_plan: "pending",
  claw_execute: "pending",
  result_evaluate: "pending",
});

const stateByDigit: Record<string, MachineState> = {
  "1": "attract",
  "2": "listening",
  "3": "thinking",
  "4": "moving",
  "5": "dropping",
  "6": "success",
  "7": "failure",
  "8": "error",
};

type StoreActions = {
  setMachineState: (state: MachineState) => void;
  setWsStatus: (status: DisplayStore["wsStatus"]) => void;
  emitEffect: (effect: EventEffect) => void;
  applyEvent: (event: DisplayEvent) => void;
  debugSetStateByDigit: (digit: string) => void;
  debugInjectTranscript: () => void;
  debugAdvanceStep: () => void;
};

export const useDisplayStore = create<DisplayStore & StoreActions>((set, get) => ({
  machineState: "attract",
  transcript: "",
  isTranscriptFinal: false,
  moodOverride: undefined,
  currentStep: undefined,
  stepStatuses: initialStepStatuses(),
  target: undefined,
  lastMotion: undefined,
  lastResult: undefined,
  activeEffect: undefined,
  effectNonce: 0,
  wsStatus: "disconnected",

  setMachineState: (state) => {
    set({ machineState: state });
  },

  setWsStatus: (status) => {
    set({ wsStatus: status });
  },

  emitEffect: (effect) => {
    set((curr) => ({
      activeEffect: effect,
      effectNonce: curr.effectNonce + 1,
    }));
  },

  applyEvent: (event) => {
    const emitEffect = get().emitEffect;

    switch (event.type) {
      case "state": {
        set({ machineState: event.state });
        return;
      }
      case "emotion": {
        set({ moodOverride: event.mood });
        return;
      }
      case "emotion_clear": {
        set({ moodOverride: undefined });
        return;
      }
      case "transcript": {
        set({ transcript: event.text, isTranscriptFinal: event.isFinal });
        return;
      }
      case "agent_step": {
        set((curr) => ({
          currentStep: event.step,
          stepStatuses: {
            ...curr.stepStatuses,
            [event.step]: event.status,
          },
        }));
        return;
      }
      case "claw_motion": {
        set({ lastMotion: { direction: event.direction, speed: event.speed } });
        emitEffect("moveStarted");
        return;
      }
      case "target": {
        set({ target: { label: event.label, confidence: event.confidence } });
        return;
      }
      case "result": {
        set({
          machineState: event.outcome,
          lastResult: { outcome: event.outcome, label: event.label },
        });
        emitEffect(event.outcome === "success" ? "grabSuccess" : "grabFailure");
        return;
      }
      case "effect": {
        emitEffect(event.effect);
        return;
      }
      default: {
        return;
      }
    }
  },

  debugSetStateByDigit: (digit) => {
    const mapped = stateByDigit[digit];
    if (!mapped) {
      return;
    }

    const stepStatuses = initialStepStatuses();
    if (mapped !== "attract" && mapped !== "listening") {
      stepStatuses.speech_to_text = "complete";
      stepStatuses.intent_parse = "complete";
    }
    if (mapped === "thinking") {
      stepStatuses.target_select = "active";
    }
    if (mapped === "moving" || mapped === "dropping") {
      stepStatuses.target_select = "complete";
      stepStatuses.motion_plan = "complete";
      stepStatuses.claw_execute = "active";
    }
    if (mapped === "success" || mapped === "failure" || mapped === "error") {
      for (const step of agentSteps) {
        stepStatuses[step] = "complete";
      }
      if (mapped === "error") {
        stepStatuses.result_evaluate = "error";
      }
    }

    set({
      machineState: mapped,
      stepStatuses,
      currentStep: undefined,
      transcript:
        mapped === "attract"
          ? ""
          : "grab the green one near the front left",
      isTranscriptFinal: mapped !== "listening",
      target:
        mapped === "attract"
          ? undefined
          : {
              label: "Green Capsule",
              confidence:
                mapped === "listening" ? 0.42 : mapped === "thinking" ? 0.78 : 0.91,
            },
      lastResult:
        mapped === "success"
          ? { outcome: "success", label: "Green Capsule" }
          : mapped === "failure"
            ? { outcome: "failure", label: "Green Capsule" }
            : undefined,
    });

    if (mapped === "success") {
      get().emitEffect("grabSuccess");
    }
    if (mapped === "failure") {
      get().emitEffect("grabFailure");
    }
    if (mapped === "error") {
      get().emitEffect("error");
    }
  },

  debugInjectTranscript: () => {
    set({
      transcript: "move right, now forward, and drop on the blue duck",
      isTranscriptFinal: true,
      machineState: "transcribing",
    });
    get().emitEffect("commandParsed");
  },

  debugAdvanceStep: () => {
    const current = get();
    const idx = current.currentStep
      ? Math.max(0, agentSteps.indexOf(current.currentStep))
      : -1;
    const next = agentSteps[(idx + 1) % agentSteps.length];

    const stepStatuses = { ...current.stepStatuses };
    for (const step of agentSteps) {
      if (step === next) {
        stepStatuses[step] = "active";
      } else if (agentSteps.indexOf(step) < agentSteps.indexOf(next)) {
        stepStatuses[step] = "complete";
      }
    }

    set({ currentStep: next, stepStatuses });
  },
}));
