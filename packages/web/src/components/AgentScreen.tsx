import { useDisplayStore } from "../state/displayStore";
import type { KeyboardTextOverlay } from "../hooks/useKeyboardTextInput";
import type { SpriteMood } from "../types/display";
import { KeyboardDialog } from "./KeyboardDialog";
import { VerticalTVFrame } from "./VerticalTVFrame";
import { EventEffectsLayer } from "./layers/EventEffectsLayer";
import { InterruptScrollerLayer } from "./layers/InterruptScrollerLayer";
import { SpriteStage } from "./layers/SpriteStage";

type AgentScreenProps = {
  mood?: SpriteMood;
  keyboardOverlay: KeyboardTextOverlay;
};

export const AgentScreen = ({ mood, keyboardOverlay }: AgentScreenProps) => {
  const machineState = useDisplayStore((s) => s.machineState);

  return (
    <VerticalTVFrame>
      <main className={`react-display react-display--${machineState}`}>
        <section className="screen-viewport">
          <div className="layer react-signal-field" aria-hidden="true" />
          <InterruptScrollerLayer />
          <SpriteStage mood={mood} />
          <KeyboardDialog overlay={keyboardOverlay} />
          <EventEffectsLayer />
        </section>
      </main>
    </VerticalTVFrame>
  );
};
