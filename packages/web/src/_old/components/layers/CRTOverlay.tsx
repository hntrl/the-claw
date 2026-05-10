export const CRTOverlay = () => {
  return (
    <div className="layer crt-overlay" aria-hidden="true">
      <div className="scanlines" />
      <div className="noise" />
      <div className="vignette" />
    </div>
  );
};
