import type { KeyboardTextOverlay } from "../hooks/useKeyboardTextInput";

type KeyboardDialogProps = {
  overlay: KeyboardTextOverlay;
};

export const KeyboardDialog = ({ overlay }: KeyboardDialogProps) => {
  if (!overlay.visible) {
    return null;
  }

  return (
    <section
      className="layer keyboard-dialog keyboard-dialog--screen"
      aria-live="polite"
      aria-label="Keyboard input dialog"
    >
      <p className="keyboard-dialog__line">
        <span>{overlay.text}</span>
        <span className="keyboard-dialog__cursor" aria-hidden="true">
          |
        </span>
      </p>
    </section>
  );
};
