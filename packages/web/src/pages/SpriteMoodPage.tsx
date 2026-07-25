import { useEffect, useMemo, useState } from "react";
import { fallbackSpriteMood, spriteByMood } from "../state/spriteAssets";
import { spriteMoods, type SpriteMood } from "../types/display";

const ROTATION_MS = 1300;

export default function SpriteMoodPage() {
  const [index, setIndex] = useState(0);
  const [selectedMood, setSelectedMood] = useState<SpriteMood | undefined>(undefined);
  const [isRotating, setIsRotating] = useState(true);

  useEffect(() => {
    if (!isRotating || selectedMood) {
      return;
    }

    const timer = window.setInterval(() => {
      setIndex((curr) => (curr + 1) % spriteMoods.length);
    }, ROTATION_MS);

    return () => window.clearInterval(timer);
  }, [isRotating, selectedMood]);

  const activeMood = selectedMood ?? spriteMoods[index];
  const spriteSrc = useMemo(
    () => spriteByMood[activeMood] ?? spriteByMood[fallbackSpriteMood],
    [activeMood],
  );

  const handleMoodSelect = (mood: SpriteMood) => {
    setSelectedMood(mood);
    setIsRotating(false);
  };

  const handleResumeRotation = () => {
    setSelectedMood(undefined);
    setIsRotating(true);
  };

  return (
    <main className="mood-page">
      <section className="mood-stage">
        {spriteSrc ? (
          <div className="mood-stage-sprite-wrap">
            <img src={spriteSrc} alt="" className="mood-stage-sprite mood-stage-sprite-bloom" draggable={false} />
            <img src={spriteSrc} alt="" className="mood-stage-sprite mood-stage-sprite-red" draggable={false} />
            <img src={spriteSrc} alt="" className="mood-stage-sprite mood-stage-sprite-blue" draggable={false} />
            <img
              src={spriteSrc}
              alt={`Parrot mood ${activeMood}`}
              className="mood-stage-sprite mood-stage-sprite-base"
              draggable={false}
            />
            <span className="sprite-crt-mask" />
            <span className="sprite-crt-glow" />
          </div>
        ) : null}
        <p className="mood-stage-label">{activeMood}</p>
      </section>

      <aside className="mood-sidebar">
        <button
          type="button"
          className={`mood-button mood-button-resume ${selectedMood ? "" : "is-active"}`}
          onClick={handleResumeRotation}
        >
          rotate all
        </button>
        {spriteMoods.map((mood) => (
          <button
            key={mood}
            type="button"
            className={`mood-button ${activeMood === mood ? "is-active" : ""}`}
            onClick={() => handleMoodSelect(mood)}
          >
            {mood}
          </button>
        ))}
      </aside>
    </main>
  );
}
