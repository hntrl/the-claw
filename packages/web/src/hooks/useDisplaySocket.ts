import { useCallback, useEffect, useRef } from "react";
import type { DisplayEvent } from "../types/display";
import { useDisplayStore } from "../state/displayStore";

const parseMessage = (payload: string): DisplayEvent[] => {
  const parsed = JSON.parse(payload) as DisplayEvent | DisplayEvent[];
  return Array.isArray(parsed) ? parsed : [parsed];
};

const MAX_PENDING_TEXTS = 32;

export const useDisplaySocket = () => {
  const applyEvent = useDisplayStore((s) => s.applyEvent);
  const setWsStatus = useDisplayStore((s) => s.setWsStatus);
  const wsRef = useRef<WebSocket | null>(null);
  const pendingTextRef = useRef<string[]>([]);

  const flushPendingText = useCallback((ws: WebSocket) => {
    while (pendingTextRef.current.length > 0) {
      const text = pendingTextRef.current[0];
      try {
        ws.send(JSON.stringify({ type: "raw_text", text }));
      } catch {
        return;
      }
      pendingTextRef.current.shift();
    }
  }, []);

  const sendRawText = useCallback((text: string): boolean => {
    const normalized = text.trim();
    if (!normalized) {
      return false;
    }

    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (pendingTextRef.current.length < MAX_PENDING_TEXTS) {
        pendingTextRef.current.push(normalized);
      }
      return true;
    }

    try {
      ws.send(JSON.stringify({ type: "raw_text", text: normalized }));
    } catch {
      if (pendingTextRef.current.length < MAX_PENDING_TEXTS) {
        pendingTextRef.current.push(normalized);
      }
    }
    return true;
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const wsFromQuery = params.get("ws");
    const configuredUrl = wsFromQuery ?? import.meta.env.VITE_DISPLAY_WS_URL;
    const defaultUrl = `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.hostname}:8787`;
    const wsUrl = configuredUrl || defaultUrl;

    let ws: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let backoffMs = 700;
    let closedByEffect = false;

    const connect = () => {
      setWsStatus("connecting");

      ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        backoffMs = 700;
        const openedWs = ws;
        if (openedWs) {
          flushPendingText(openedWs);
        }
        setWsStatus("connected");
      };

      ws.onmessage = (event) => {
        if (typeof event.data !== "string") {
          return;
        }

        try {
          for (const packet of parseMessage(event.data)) {
            applyEvent(packet);
          }
        } catch {
          // Ignore malformed packets so a single bad event does not kill the display.
        }
      };

      ws.onerror = () => {
        setWsStatus("disconnected");
      };

      ws.onclose = () => {
        if (wsRef.current === ws) {
          wsRef.current = null;
        }
        setWsStatus("disconnected");
        if (closedByEffect) {
          return;
        }

        reconnectTimer = window.setTimeout(() => {
          connect();
          backoffMs = Math.min(backoffMs * 1.6, 8_000);
        }, backoffMs);
      };
    };

    connect();

    return () => {
      closedByEffect = true;
      wsRef.current = null;
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      ws?.close();
      setWsStatus("disconnected");
    };
  }, [applyEvent, flushPendingText, setWsStatus]);

  return { sendRawText };
};
