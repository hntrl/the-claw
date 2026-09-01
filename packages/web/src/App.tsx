import { useMemo } from "react";
import { AgentScreen } from "./components/AgentScreen";
import { DebugFieldPanel } from "./components/DebugFieldPanel";
import { useKeyboardDebug } from "./hooks/useKeyboardDebug";
import { useDisplaySocket } from "./hooks/useDisplaySocket";
import { useKeyboardTextInput } from "./hooks/useKeyboardTextInput";
import { spriteMoods, type SpriteMood } from "./types/display";

function App() {
  const { isDebug, moodOverride } = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    const mood = params.get("mood");

    return {
      isDebug: params.has("debug"),
      moodOverride: spriteMoods.includes(mood as SpriteMood)
        ? (mood as SpriteMood)
        : undefined,
    };
  }, []);

  useKeyboardDebug(isDebug);
  const { sendRawText } = useDisplaySocket();
  const keyboardOverlay = useKeyboardTextInput(sendRawText);

  return (
    <>
      <AgentScreen mood={moodOverride} keyboardOverlay={keyboardOverlay} />
      {isDebug && <DebugFieldPanel />}
    </>
  );
}

export default App;
