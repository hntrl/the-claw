import { useEffect, useState } from "react";
import { useDisplayStore } from "../../state/displayStore";
import type { EventEffect } from "../../types/display";

const classByEffect: Record<EventEffect, string> = {
  voiceDetected: "fx-voice",
  commandParsed: "fx-command",
  moveStarted: "fx-move",
  dropStarted: "fx-drop",
  grabSuccess: "fx-success",
  grabFailure: "fx-failure",
  error: "fx-error",
};

export const EventEffectsLayer = () => {
  const activeEffect = useDisplayStore((s) => s.activeEffect);
  const effectNonce = useDisplayStore((s) => s.effectNonce);
  const [effectClass, setEffectClass] = useState<string>("");

  useEffect(() => {
    if (!activeEffect) {
      return;
    }

    const fx = classByEffect[activeEffect] ?? "";
    setEffectClass("");
    const frame = window.requestAnimationFrame(() => setEffectClass(fx));
    const timer = window.setTimeout(() => setEffectClass(""), 700);

    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [activeEffect, effectNonce]);

  return (
    <div className={`layer event-effects ${effectClass}`} aria-hidden="true">
      <span className="ring" />
      <span className="ring ring-b" />
      <span className="flash" />
      <span className="shake" />
    </div>
  );
};
