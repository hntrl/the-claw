import { useMemo } from "react";
import { AgentScreen } from "./components/AgentScreen";
import { DebugFieldPanel } from "./components/DebugFieldPanel";
import { useClientMicStream } from "./hooks/useClientMicStream";
import { useKeyboardDebug } from "./hooks/useKeyboardDebug";
import { useDisplaySocket } from "./hooks/useDisplaySocket";
import { useKeyboardTextInput } from "./hooks/useKeyboardTextInput";
import { spriteMoods, type SpriteMood } from "./types/display";

function App() {
  const moodOverride = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    const mood = params.get("mood");
    if (!mood) {
      return undefined;
    }

    if (spriteMoods.includes(mood as SpriteMood)) {
      return mood as SpriteMood;
    }

    return undefined;
  }, []);

  useKeyboardDebug();
  const { sendRawText, sendAudioChunk } = useDisplaySocket();
  const mic = useClientMicStream(sendAudioChunk);
  const keyboardOverlay = useKeyboardTextInput(sendRawText);
  const micButtonClass = !mic.supported
    ? ""
    : mic.mode === "on" || mic.pttActive
      ? "is-live"
      : mic.mode === "ptt"
        ? "is-ptt"
        : "is-muted";
  const micLabel = !mic.supported
    ? "MIC N/A"
    : mic.mode === "off"
      ? "MIC OFF"
      : mic.mode === "on"
        ? "MIC ON"
        : mic.pttActive
          ? "PTT LIVE"
          : "PTT (HOLD R-OPT)";
  const micAriaLabel = !mic.supported
    ? "Microphone unavailable"
    : mic.mode === "off"
      ? "Microphone off"
      : mic.mode === "on"
        ? "Microphone always on"
        : "Push to talk mode. Hold right Option key to transmit";

  return (
    <>
      <AgentScreen mood={moodOverride} keyboardOverlay={keyboardOverlay} />
      <button
        aria-label={micAriaLabel}
        className={`mic-toggle ${micButtonClass}`}
        disabled={!mic.supported}
        onClick={mic.cycleMode}
        type="button"
      >
        {micLabel}
      </button>
      {mic.error ? <p className="mic-error">{mic.error}</p> : null}
      <DebugFieldPanel />
    </>
  );
}

export default App;
