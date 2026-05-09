import { useCallback, useEffect, useRef } from "react";
import type { DisplayEvent } from "../types/display";
import { useDisplayStore } from "../state/displayStore";

const parseMessage = (payload: string): DisplayEvent[] => {
  const parsed = JSON.parse(payload) as DisplayEvent | DisplayEvent[];
  return Array.isArray(parsed) ? parsed : [parsed];
};

export const useDisplaySocket = () => {
  const applyEvent = useDisplayStore((s) => s.applyEvent);
  const setWsStatus = useDisplayStore((s) => s.setWsStatus);
  const wsRef = useRef<WebSocket | null>(null);

  const sendRawText = useCallback((text: string): boolean => {
    const normalized = text.trim();
    if (!normalized) {
      return false;
    }

    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return false;
    }

    ws.send(JSON.stringify({ type: "raw_text", text: normalized }));
    return true;
  }, []);

  const sendAudioChunk = useCallback((chunk: Uint8Array): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return false;
    }
    const payload = chunk.buffer.slice(
      chunk.byteOffset,
      chunk.byteOffset + chunk.byteLength,
    );
    ws.send(payload);
    return true;
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const wsFromQuery = params.get("ws");
    const wsUrl = wsFromQuery ?? import.meta.env.VITE_DISPLAY_WS_URL;

    if (!wsUrl) {
      return;
    }

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
  }, [applyEvent, setWsStatus]);

  return { sendRawText, sendAudioChunk };
};
