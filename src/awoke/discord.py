from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from awoke import __version__

LOG = logging.getLogger(__name__)


class DiscordRateLimit(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Discord rate limit; retry after {retry_after} seconds")
        self.retry_after = retry_after


class DiscordClient:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url + ("&wait=true" if "?" in webhook_url else "?wait=true")

    async def send(self, content: str, image: bytes | None = None) -> str | None:
        return await self._with_rate_limit("POST", self.webhook_url, content, image)

    async def edit(self, message_id: str, content: str, image: bytes | None = None) -> None:
        url = self.webhook_url.split("?wait=true")[0].split("&wait=true")[0] + f"/messages/{message_id}"
        await self._with_rate_limit("PATCH", url, content, image)

    async def edit_content(self, message_id: str, content: str) -> None:
        url = self.webhook_url.split("?wait=true")[0].split("&wait=true")[0] + f"/messages/{message_id}"
        await self._with_rate_limit("PATCH", url, content, None, preserve_attachments=True)

    async def _with_rate_limit(
        self, method: str, url: str, content: str, image: bytes | None, preserve_attachments: bool = False,
    ) -> str | None:
        for attempt in range(5):
            try:
                if preserve_attachments:
                    return await asyncio.to_thread(self._request, method, url, content, image, True)
                return await asyncio.to_thread(self._request, method, url, content, image)
            except DiscordRateLimit as error:
                if attempt == 4:
                    raise
                await asyncio.sleep(error.retry_after)
        raise RuntimeError("Discord request retry loop exited unexpectedly")

    def _request(
        self, method: str, url: str, content: str, image: bytes | None, preserve_attachments: bool = False,
    ) -> str | None:
        payload: dict[str, Any] = {"content": content}
        if method == "PATCH" and not preserve_attachments:
            payload["attachments"] = []
        if image is None:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            boundary = f"AwokeBoundary{int(time.time() * 1000)}"
            json_part = json.dumps(payload).encode("utf-8")
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                "Content-Type: application/json\r\n\r\n"
            ).encode() + json_part + (
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"map.png\"\r\n"
                "Content-Type: image/png\r\n\r\n"
            ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        headers["User-Agent"] = f"DiscordBot (https://github.com/zaki0220/Awoke, {__version__})"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response_body = response.read()
                if method == "POST":
                    result = json.loads(response_body)
                    return str(result["id"])
                return None
        except urllib.error.HTTPError as error:
            detail = error.read(2048).decode("utf-8", "replace")
            if error.code == 429:
                raise DiscordRateLimit(_retry_after(detail)) from None
            raise RuntimeError(f"Discord returned HTTP {error.code}: {detail}") from None


def _retry_after(body: str) -> float:
    try:
        return float(json.loads(body).get("retry_after", 1.0))
    except (ValueError, TypeError, AttributeError):
        return 1.0
