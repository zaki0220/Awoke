from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
import pytest

from awoke import __version__
from awoke.config import webhook_credential
from awoke.discord import DiscordClient


@pytest.mark.parametrize("host", ["discord.com", "canary.discord.com"])
def test_webhook_credential_supports_discord_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    url = f"https://{host}/api/webhooks/123456/fake-token"
    (tmp_path / "discord-webhook-url").write_text(url + "\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert webhook_credential() == url


@pytest.mark.parametrize(
    "url",
    [
        "http://canary.discord.com/api/webhooks/123456/fake-token",
        "https://canary.discord.com.example.com/api/webhooks/123456/fake-token",
        "https://example.com/api/webhooks/123456/fake-token",
        "https://canary.discord.com/other/123456/fake-token",
    ],
)
def test_webhook_credential_rejects_other_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    (tmp_path / "discord-webhook-url").write_text(url, encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    with pytest.raises(ValueError, match="unexpected URL format"):
        webhook_credential()


def test_canary_webhook_send_and_edit_use_same_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://canary.discord.com/api/webhooks/123456/fake-token"
    (tmp_path / "discord-webhook-url").write_text(url, encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    client = DiscordClient(webhook_credential())
    requests: list[tuple[str, str]] = []

    def request(method: str, request_url: str, content: str, image: bytes | None) -> str | None:
        requests.append((method, request_url))
        return "987654" if method == "POST" else None

    monkeypatch.setattr(client, "_request", request)

    async def scenario() -> None:
        message_id = await client.send("test")
        assert message_id == "987654"
        await client.edit(message_id, "updated")

    asyncio.run(scenario())
    assert requests == [("POST", f"{url}?wait=true"), ("PATCH", f"{url}/messages/987654")]


@pytest.mark.parametrize("method,image", [("POST", None), ("PATCH", b"fake-png")])
def test_discord_requests_include_bot_user_agent(
    monkeypatch: pytest.MonkeyPatch, method: str, image: bytes | None
) -> None:
    requests: list[urllib.request.Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps({"id": "987654"}).encode()

    def open_request(request: urllib.request.Request, timeout: int) -> Response:
        requests.append(request)
        return Response()

    monkeypatch.setattr("awoke.discord.urllib.request.urlopen", open_request)
    client = DiscordClient("https://canary.discord.com/api/webhooks/123456/fake-token")
    client._request(method, client.webhook_url, "test", image)
    assert requests[0].get_header("User-agent") == (
        f"DiscordBot (https://github.com/zaki0220/Awoke, {__version__})"
    )


@pytest.mark.parametrize("image", [None, b"fake-png"])
def test_edit_removes_old_attachments(monkeypatch: pytest.MonkeyPatch, image: bytes | None) -> None:
    requests: list[urllib.request.Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"{}"

    def open_request(request: urllib.request.Request, timeout: int) -> Response:
        requests.append(request)
        return Response()

    monkeypatch.setattr("awoke.discord.urllib.request.urlopen", open_request)
    client = DiscordClient("https://discord.com/api/webhooks/123456/fake-token")
    client._request("PATCH", client.webhook_url, "updated", image)
    body = requests[0].data
    assert body is not None
    if image is None:
        assert json.loads(body)["attachments"] == []
    else:
        assert b'"attachments": []' in body
        assert b'filename="map.png"' in body
