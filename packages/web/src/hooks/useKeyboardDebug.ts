import { useEffect } from "react";
import { useDisplayStore } from "../state/displayStore";

export const useKeyboardDebug = (enabled: boolean) => {
  const debugSetStateByDigit = useDisplayStore((s) => s.debugSetStateByDigit);
  const debugInjectTranscript = useDisplayStore((s) => s.debugInjectTranscript);
  const debugAdvanceStep = useDisplayStore((s) => s.debugAdvanceStep);
  const emitEffect = useDisplayStore((s) => s.emitEffect);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") {
        return;
      }

      if (event.key >= "1" && event.key <= "8") {
        debugSetStateByDigit(event.key);
      }

      if (event.key === "t" || event.key === "T") {
        debugInjectTranscript();
      }

      if (event.key === "n" || event.key === "N") {
        debugAdvanceStep();
      }

      if (event.key === "m" || event.key === "M") {
        emitEffect("moveStarted");
      }

      if (event.key === "d" || event.key === "D") {
        emitEffect("dropStarted");
      }

      if (event.key === "v" || event.key === "V") {
        emitEffect("voiceDetected");
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [debugAdvanceStep, debugInjectTranscript, debugSetStateByDigit, emitEffect, enabled]);
};
