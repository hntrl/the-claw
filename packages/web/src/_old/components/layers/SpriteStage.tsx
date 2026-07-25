import { useEffect, useMemo, useState } from "react";
import { fallbackSpriteMood, spriteByMood } from "../../state/spriteAssets";
import { useDisplayStore } from "../../state/displayStore";
import { visualStateMap } from "../../state/visualStateMap";
import type { SpriteMood } from "../../types/display";

const ATTRACT_BLINK_INTERVAL_MS = 5200;
const ATTRACT_BLINK_DURATION_MS = 120;

type SpriteStageProps = {
  mood?: SpriteMood;
};

export const SpriteStage = ({ mood }: SpriteStageProps) => {
  const machineState = useDisplayStore((s) => s.machineState);
  const moodOverride = useDisplayStore((s) => s.moodOverride);
  const mappedMood = visualStateMap[machineState].sprite;
  const [isBlinking, setIsBlinking] = useState(false);

  useEffect(() => {
    if (mood || moodOverride || machineState !== "attract") {
      setIsBlinking(false);
      return;
    }

    let blinkTimer = 0;
    let resetTimer = 0;
    const scheduleBlink = () => {
      blinkTimer = window.setTimeout(() => {
        setIsBlinking(true);
        resetTimer = window.setTimeout(() => {
          setIsBlinking(false);
          scheduleBlink();
        }, ATTRACT_BLINK_DURATION_MS);
      }, ATTRACT_BLINK_INTERVAL_MS);
    };
    scheduleBlink();

    return () => {
      window.clearTimeout(blinkTimer);
      window.clearTimeout(resetTimer);
    };
  }, [machineState, mood, moodOverride]);

  const resolvedMood = mood ?? moodOverride ?? (machineState === "attract" ? (isBlinking ? "blink" : "calm") : mappedMood);

  const spriteSrc = useMemo(() => {
    return spriteByMood[resolvedMood] ?? spriteByMood[fallbackSpriteMood];
  }, [resolvedMood]);

  return (
    <section className="layer sprite-stage">
      <div className="sprite-orbit" />
      <div className="sprite-frame">
        {spriteSrc ? (
          <div className="sprite-crt-wrap">
            <img src={spriteSrc} alt="" className="sprite-image sprite-image-bloom" draggable={false} />
            <img src={spriteSrc} alt="" className="sprite-image sprite-image-red" draggable={false} />
            <img src={spriteSrc} alt="" className="sprite-image sprite-image-blue" draggable={false} />
            <img
              src={spriteSrc}
              alt={`Parrot mascot mood: ${resolvedMood}`}
              className="sprite-image sprite-image-base"
              draggable={false}
            />
            <span className="sprite-crt-mask" />
            <span className="sprite-crt-glow" />
          </div>
        ) : (
          <div className="sprite-fallback">sprite missing</div>
        )}
      </div>
    </section>
  );
};
