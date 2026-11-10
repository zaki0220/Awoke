from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from awoke import __version__

LOG = logging.getLogger(__name__)
WS_URL = "wss://api.p2pquake.net/v2/ws"
SANDBOX_WS_URL = "wss://api-realtime-sandbox.p2pquake.net/v2/ws"
HISTORY_URL = "https://api.p2pquake.net/v2/history"
WATCH_CODES = {551, 552, 556}


async def fetch_history() -> list[dict[str, Any]]:
    query = urllib.parse.urlencode([("codes", code) for code in sorted(WATCH_CODES)] + [("limit", 100)])
    url = f"{HISTORY_URL}?{query}"

    def request() -> list[dict[str, Any]]:
        req = urllib.request.Request(url, headers={"User-Agent": f"Awoke/{__version__}"})
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, list):
            raise ValueError("P2P history response was not a JSON array")
        return [item for item in payload if isinstance(item, dict)]

    return await asyncio.to_thread(request)


async def open_stream(test_mode: bool = False) -> ClientConnection:
    url = SANDBOX_WS_URL if test_mode else WS_URL
    return await connect(url, open_timeout=20, ping_interval=20, ping_timeout=20, max_size=2**20)


async def stream_reader(socket: ClientConnection, queue: asyncio.Queue[dict[str, Any]]) -> None:
    async for frame in socket:
        try:
            payload = json.loads(frame)
        except (json.JSONDecodeError, TypeError):
            LOG.warning("Ignoring malformed WebSocket JSON frame")
            continue
        if not isinstance(payload, dict):
            continue
        code = payload.get("code")
        if code not in WATCH_CODES:
            continue
        await queue.put(payload)
