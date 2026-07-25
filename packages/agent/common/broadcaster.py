from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any


class DisplayBroadcaster:
    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

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

        stale: list[Any] = []
        for client in clients:
            try:
                await client.send(message)
            except Exception:
                stale.append(client)

        if stale:
            async with self._lock:
                for client in stale:
                    self._clients.discard(client)

    async def snapshot_clients(self) -> Iterable[Any]:
        async with self._lock:
            return tuple(self._clients)

