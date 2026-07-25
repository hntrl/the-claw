import type { PropsWithChildren } from "react";

export const VerticalTVFrame = ({ children }: PropsWithChildren) => {
  return (
    <div className="tv-stage">
      <svg className="crt-shape-defs" focusable="false" aria-hidden="true">
        <defs>
          <clipPath id="crt-screen-clip" clipPathUnits="objectBoundingBox">
            <path d="M .045 .045 Q .5 .018 .955 .045 Q 1.025 .5 .955 .955 Q .5 .982 .045 .955 Q -.025 .5 .045 .045 Z" />
          </clipPath>
        </defs>
      </svg>
      <div className="tv-shell">
        <div className="crt-bloom crt-bloom--wide" aria-hidden="true">
          <span />
        </div>
        <div className="crt-bloom crt-bloom--core" aria-hidden="true">
          <span />
        </div>
        <div className="tv-frame">
          <div className="crt-content">{children}</div>
        </div>
      </div>
    </div>
  );
};
