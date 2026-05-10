import interrupt26LogoSrc from "../../../sprites/interrupt26.png";

const ITEM_COUNT = 12;

export const InterruptScrollerLayer = () => {
  return (
    <div className="layer interrupt-strip" aria-hidden="true">
      <div className="interrupt-strip__track">
        {Array.from({ length: ITEM_COUNT }).map((_, index) => (
          <img
            className="interrupt-strip__logo"
            src={interrupt26LogoSrc}
            alt=""
            draggable={false}
            key={index}
          />
        ))}
      </div>
    </div>
  );
};
