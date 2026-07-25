import { useMemo } from "react";
import { AgentScreen } from "./components/AgentScreen";
import { DebugFieldPanel } from "./components/DebugFieldPanel";
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
  const { sendRawText } = useDisplaySocket();
  const keyboardOverlay = useKeyboardTextInput(sendRawText);

  return (
    <>
      <AgentScreen mood={moodOverride} keyboardOverlay={keyboardOverlay} />
      <DebugFieldPanel />
    </>
  );
}

export default App;
