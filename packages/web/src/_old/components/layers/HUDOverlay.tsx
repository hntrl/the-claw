import { useDisplayStore } from "../../state/displayStore";
import { visualStateMap } from "../../state/visualStateMap";

export const HUDOverlay = () => {
  const machineState = useDisplayStore((s) => s.machineState);
  const transcript = useDisplayStore((s) => s.transcript);
  const target = useDisplayStore((s) => s.target);
  const visual = visualStateMap[machineState];

  return (
    <>
      <header className="layer hud-top">
        <p className="brand-mark">INTERRUPT26</p>
        <p className="system-mark">AGENT CLAW</p>
      </header>

      <section className="layer hud-bottom">
        <p className="state-code">{machineState}</p>
        <h2>{visual.headline}</h2>

        <div className="readout">
          <p>{visual.showTranscript ? transcript || "...awaiting speech" : "say a command to begin"}</p>
        </div>

        {target ? (
          <p className="target-line">
            target / {target.label}
            {typeof target.confidence === "number" ? ` (${Math.round(target.confidence * 100)}%)` : ""}
          </p>
        ) : null}
      </section>
    </>
  );
};
