import { useMemo } from "react";
import { DebugFieldPanel } from "./components/DebugFieldPanel";
import { VerticalTVFrame } from "./components/VerticalTVFrame";
import { VectorCRTCanvas } from "./components/VectorCRTCanvas";
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
  const sendRawText = useDisplaySocket();
  const keyboardOverlay = useKeyboardTextInput(sendRawText);

  return (
    <>
      <VerticalTVFrame>
        <VectorCRTCanvas mood={moodOverride} keyboardOverlay={keyboardOverlay} />
      </VerticalTVFrame>
      <DebugFieldPanel />
    </>
  );
}

export default App;
