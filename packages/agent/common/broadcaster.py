from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any


class DisplayBroadcaster:
    def __init__(self, *, send_timeout_s: float = 2.0) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()
        self._send_timeout_s = max(0.1, send_timeout_s)

    async def add_client(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.add(websocket)

    async def remove_client(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        async with self._lock:
            clients = list(self._clients)

        async def send(client: Any) -> tuple[Any, bool]:
            try:
                await asyncio.wait_for(
                    client.send(message), timeout=self._send_timeout_s
                )
            except Exception:
                return client, False
            return client, True

        results = await asyncio.gather(*(send(client) for client in clients))
        stale = [client for client, delivered in results if not delivered]
        if stale:
            async with self._lock:
                self._clients.difference_update(stale)

            async def close(client: Any) -> None:
                close_client = getattr(client, "close", None)
                if close_client is None:
                    return
                try:
                    await asyncio.wait_for(close_client(), timeout=self._send_timeout_s)
                except Exception:
                    pass

            await asyncio.gather(*(close(client) for client in stale))

    async def snapshot_clients(self) -> Iterable[Any]:
        async with self._lock:
            return tuple(self._clients)
