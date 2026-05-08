import { useCallback, useEffect, useRef, useState } from "react";

export type KeyboardTextOverlay = {
  visible: boolean;
  text: string;
};

const DEFAULT_INACTIVITY_MS = 4200;

const isTypingTarget = (target: EventTarget | null): boolean => {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || target.isContentEditable;
};

const isPrintableKey = (event: KeyboardEvent): boolean => {
  if (event.ctrlKey || event.metaKey || event.altKey) {
    return false;
  }
  return event.key.length === 1;
};

export const useKeyboardTextInput = (
  sendRawText: (text: string) => boolean,
  inactivityMs: number = DEFAULT_INACTIVITY_MS,
): KeyboardTextOverlay => {
  const [visible, setVisible] = useState(false);
  const [text, setText] = useState("");
  const hideTimerRef = useRef<number | undefined>(undefined);

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current) {
      window.clearTimeout(hideTimerRef.current);
      hideTimerRef.current = undefined;
    }
  }, []);

  const dismiss = useCallback(() => {
    clearHideTimer();
    setVisible(false);
    setText("");
  }, [clearHideTimer]);

  const scheduleHide = useCallback(() => {
    clearHideTimer();
    hideTimerRef.current = window.setTimeout(() => {
      setVisible(false);
      setText("");
    }, inactivityMs);
  }, [clearHideTimer, inactivityMs]);

  const submit = useCallback(() => {
    const normalized = text.trim();
    if (!normalized) {
      dismiss();
      return;
    }

    if (sendRawText(normalized)) {
      dismiss();
      return;
    }
    scheduleHide();
  }, [dismiss, scheduleHide, sendRawText, text]);

  useEffect(() => {
    return () => clearHideTimer();
  }, [clearHideTimer]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTypingTarget(event.target)) {
        return;
      }

      if (event.key === "Escape" && visible) {
        event.preventDefault();
        event.stopPropagation();
        dismiss();
        return;
      }

      if (event.key === "Enter" && visible) {
        event.preventDefault();
        event.stopPropagation();
        submit();
        return;
      }

      if (event.key === "Backspace" && visible) {
        event.preventDefault();
        event.stopPropagation();
        setText((current) => current.slice(0, -1));
        scheduleHide();
        return;
      }

      if (!isPrintableKey(event)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      setVisible(true);
      setText((current) => current + event.key);
      scheduleHide();
    };

    const onPaste = (event: ClipboardEvent) => {
      if (isTypingTarget(event.target)) {
        return;
      }

      const pasted = event.clipboardData?.getData("text") ?? "";
      if (!pasted) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      setVisible(true);
      setText((current) => current + pasted.replace(/\s+/g, " "));
      scheduleHide();
    };

    window.addEventListener("keydown", onKeyDown, { capture: true });
    window.addEventListener("paste", onPaste, { capture: true });

    return () => {
      window.removeEventListener("keydown", onKeyDown, { capture: true });
      window.removeEventListener("paste", onPaste, { capture: true });
    };
  }, [dismiss, scheduleHide, submit, visible]);

  return { visible, text };
};
