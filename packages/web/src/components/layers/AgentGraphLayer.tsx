import { agentSteps } from "../../types/display";
import { useDisplayStore } from "../../state/displayStore";

export const AgentGraphLayer = () => {
  const stepStatuses = useDisplayStore((s) => s.stepStatuses);
  const currentStep = useDisplayStore((s) => s.currentStep);

  return (
    <div className="layer agent-graph" aria-hidden="true">
      {agentSteps.map((step, idx) => {
        const status = stepStatuses[step];
        const isActive = currentStep === step || status === "active";
        return (
          <div key={step} className={`graph-node graph-node-${status} ${isActive ? "is-active" : ""}`}>
            <span>{step.replace(/_/g, " ")}</span>
            {idx < agentSteps.length - 1 ? <i className="graph-link" /> : null}
          </div>
        );
      })}
    </div>
  );
};
