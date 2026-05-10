import type { PropsWithChildren } from "react";

export const VerticalTVFrame = ({ children }: PropsWithChildren) => {
  return (
    <div className="tv-stage">
      <div className="tv-shell">
        <div className="tv-frame">
          <div className="screen-content">{children}</div>
        </div>
      </div>
    </div>
  );
};
