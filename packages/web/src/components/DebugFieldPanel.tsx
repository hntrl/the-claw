import { useEffect, useRef, useState } from "react";
import { useDisplayStore } from "../state/displayStore";
import type { EventEffect, MachineState } from "../types/display";

type DemoMode = {
  label: string;
  state: MachineState;
  effect?: EventEffect;
};

const demoModes: DemoMode[] = [
  { label: "Idle", state: "attract" },
  { label: "Audio", state: "listening", effect: "voiceDetected" },
  { label: "Think", state: "thinking", effect: "commandParsed" },
  { label: "Target", state: "targeting" },
  { label: "Move", state: "moving", effect: "moveStarted" },
  { label: "Drop", state: "dropping", effect: "dropStarted" },
  { label: "Win", state: "success", effect: "grabSuccess" },
  { label: "Fail", state: "failure", effect: "grabFailure" },
  { label: "Error", state: "error", effect: "error" },
];

export const DebugFieldPanel = () => {
  const [isCycling, setIsCycling] = useState(false);
  const cycleIndexRef = useRef(0);
  const machineState = useDisplayStore((s) => s.machineState);
  const setMachineState = useDisplayStore((s) => s.setMachineState);
  const emitEffect = useDisplayStore((s) => s.emitEffect);

  const activateMode = (mode: DemoMode, index: number) => {
    cycleIndexRef.current = index;
    setMachineState(mode.state);
    if (mode.effect) {
      emitEffect(mode.effect);
    }
  };

  useEffect(() => {
    if (!isCycling) {
      return;
    }

    const timer = window.setInterval(() => {
      const next = (cycleIndexRef.current + 1) % demoModes.length;
      const mode = demoModes[next];
      cycleIndexRef.current = next;
      setMachineState(mode.state);
      if (mode.effect) {
        emitEffect(mode.effect);
      }
    }, 1800);

    return () => window.clearInterval(timer);
  }, [emitEffect, isCycling, setMachineState]);

  return (
    <aside className="field-debug-panel" aria-label="Foreground field debug controls">
      <div className="field-debug-panel__eyebrow">FIELD DEBUG</div>
      <button
        className={`field-debug-panel__cycle ${isCycling ? "is-active" : ""}`}
        type="button"
        onClick={() => setIsCycling((current) => !current)}
      >
        {isCycling ? "Stop cycle" : "Auto cycle"}
      </button>
      <div className="field-debug-panel__grid">
        {demoModes.map((mode, index) => (
          <button
            className={machineState === mode.state ? "is-active" : ""}
            key={mode.state}
            type="button"
            onClick={() => activateMode(mode, index)}
          >
            {mode.label}
          </button>
        ))}
      </div>
    </aside>
  );
};
