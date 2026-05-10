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

  return (
    <>
      <AgentScreen mood={moodOverride} keyboardOverlay={keyboardOverlay} />
      <button
        aria-label={mic.muted ? "Unmute microphone" : "Mute microphone"}
        className={`mic-toggle ${mic.muted ? "is-muted" : "is-live"}`}
        disabled={!mic.supported}
        onClick={mic.toggleMuted}
        type="button"
      >
        {mic.supported ? (mic.muted ? "MIC OFF" : "MIC ON") : "MIC N/A"}
      </button>
      {mic.error ? <p className="mic-error">{mic.error}</p> : null}
      <DebugFieldPanel />
    </>
  );
}

export default App;
