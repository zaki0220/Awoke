from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from awoke.config import Config
from awoke.service import EventProcessor, Service, _distance_km, _format_message, _is_expired_report
from awoke.store import Store

_JST = timezone(timedelta(hours=9))
_TEST_EVENT_TIME = datetime.now(_JST).strftime("%Y/%m/%d %H:%M:%S")


class FakeDiscord:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[tuple[str, str]] = []

    async def send(self, content: str, image: bytes | None = None) -> str:
        self.sent.append(content)
        return f"message-{len(self.sent)}"

    async def edit(self, message_id: str, content: str, image: bytes | None = None) -> None:
        self.edited.append((message_id, content))


class FakeRenderer:
    def render(self, code: int, state: dict[str, Any]) -> None:
        return None


def _processor(tmp_path: Path, threshold: int = 40) -> tuple[EventProcessor, Store, FakeDiscord]:
    config = Config(threshold=threshold, database=tmp_path / "state.sqlite3")
    store = Store(config.database)
    discord = FakeDiscord()
    return EventProcessor(config, store, discord, FakeRenderer()), store, discord


def _quake(report_id: str, kind: str, *, scale: int, lon: float = -200, lat: float = -200) -> dict[str, Any]:
    return {
        "code": 551,
        "id": report_id,
        "time": _TEST_EVENT_TIME,
        "issue": {"type": kind, "time": _TEST_EVENT_TIME, "correct": "None"},
        "earthquake": {
            "time": _TEST_EVENT_TIME,
            "maxScale": scale,
            "hypocenter": {"name": "茨城県沖" if lon != -200 else "", "longitude": lon, "latitude": lat, "depth": 40, "magnitude": 5.5},
        },
        "points": [],
    }


def test_551_reports_with_same_time_merge_and_edit_message(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    asyncio.run(processor.process(_quake("report-1", "ScalePrompt", scale=30)))
    assert not discord.sent
    update = _quake("report-2", "Destination", scale=-1, lon=142.2, lat=36.2)
    update["earthquake"]["hypocenter"]["depth"] = -1
    update["earthquake"]["hypocenter"]["magnitude"] = -1
    asyncio.run(processor.process(update))
    assert not discord.sent
    asyncio.run(processor.process(_quake("report-3", "DetailScale", scale=40, lon=142.2, lat=36.2)))
    assert len(discord.sent) == 1
    event = store.events_for_time(551, _TEST_EVENT_TIME)[0]
    assert event["state"]["max_scale"] == 40
    assert event["state"]["hypocenter"]["longitude"] == 142.2
    assert event["state"]["hypocenter"]["depth"] == 40
    assert event["state"]["hypocenter"]["magnitude"] == 5.5
    asyncio.run(processor.process(_quake("report-4", "DetailScale", scale=50, lon=142.2, lat=36.2)))
    assert discord.edited and discord.edited[-1][0] == "message-1"
    store.close()


def test_551_does_not_merge_ambiguous_time_candidates(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    first = _quake("report-1", "Destination", scale=30, lon=140.0, lat=36.0)
    second = _quake("report-2", "Destination", scale=30, lon=145.0, lat=40.0)
    third = _quake("report-3", "ScalePrompt", scale=30)
    asyncio.run(processor.process(first, notify=False))
    asyncio.run(processor.process(second, notify=False))
    asyncio.run(processor.process(third, notify=False))
    assert len(store.events_for_time(551, _TEST_EVENT_TIME)) == 3
    assert not discord.sent
    store.close()


def test_p2p_sample_report_types_are_integrated_without_clearing_fields(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio

    sample_path = Path(__file__).parents[1] / "sample" / "p2p_sample_551_20261109_4.json"
    payloads = json.loads(sample_path.read_text(encoding="utf-8-sig"))
    reports = [
        item for item in payloads
        if (item.get("earthquake") or {}).get("time") == "2026/11/09 13:52:00"
    ]
    assert [item["issue"]["type"] for item in reversed(reports)] == [
        "ScalePrompt", "ScalePrompt", "Destination", "DetailScale"
    ]
    monkeypatch.setattr("awoke.service._is_expired_report", lambda code, payload, days: False)
    processor, store, _ = _processor(tmp_path, threshold=30)
    for report in reversed(reports):
        asyncio.run(processor.process(report, notify=False))
    event = store.events_for_time(551, "2026/11/09 13:52:00")
    assert len(event) == 1
    assert event[0]["state"]["hypocenter"]["longitude"] == 142.2
    assert event[0]["state"]["max_scale"] == 30
    store.close()


def test_p2p556_serial_order_and_cancellation(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    base = {
        "code": 556, "id": "eew-1", "cancelled": False,
        "issue": {"eventId": "event-1", "serial": "2"},
        "earthquake": {"hypocenter": {"name": "茨城県沖", "longitude": 142.2, "latitude": 36.2, "magnitude": 5.2}},
        "areas": [{"name": "茨城県南部", "scaleTo": 40}],
    }
    asyncio.run(processor.process(base))
    older = dict(base, id="eew-old", issue={"eventId": "event-1", "serial": "1"}, cancelled=True)
    asyncio.run(processor.process(older))
    asyncio.run(processor.process(base))
    state = store.get_event("556:event-1")["state"]
    assert not state["cancelled"]
    assert state["issue"]["serial"] == "2"
    assert len(discord.sent) == 1
    assert len(discord.edited) == 1
    store.close()


def test_tsunami_same_report_update_edits_and_preserves_known_areas(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    base = {
        "code": 552,
        "id": "tsunami-1",
        "cancelled": False,
        "issue": {"type": "Focus"},
        "areas": [{"name": "茨城県", "grade": "Warning"}],
    }
    asyncio.run(processor.process(base))
    update = dict(base, cancelled=True, areas=[])
    asyncio.run(processor.process(update))
    state = store.get_event("552:tsunami-1")["state"]
    assert state["cancelled"]
    assert state["areas"] == base["areas"]
    assert len(discord.sent) == 1
    assert "津波警報・注意報が解除されました" in discord.edited[-1][1]
    store.close()


def test_unknown_intensity_is_not_formatted_as_zero() -> None:
    message = _format_message(551, {"event_time": _TEST_EVENT_TIME, "max_scale": None})
    assert "最大震度" not in message


def test_distance_calculation() -> None:
    assert _distance_km((140.0, 36.0), (140.0, 36.0)) == 0
    assert 80 < _distance_km((140.0, 36.0), (141.0, 36.0)) < 100


def test_reports_older_than_retention_are_log_only_candidates() -> None:
    report = _quake("old-report", "DetailScale", scale=70)
    report["earthquake"]["time"] = "2000/01/01 00:00:00"
    assert _is_expired_report(551, report, 7)
    recent = _quake("recent-report", "DetailScale", scale=70)
    assert not _is_expired_report(551, recent, 7)


def test_expired_reports_are_not_stored_or_notified(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    report = _quake("old-report", "DetailScale", scale=70)
    report["earthquake"]["time"] = "2000/01/01 00:00:00"
    asyncio.run(processor.process(report, notify=False))
    assert not store.report_seen(551, "old-report")
    assert not discord.sent
    store.close()


def test_missing_config_file_is_not_silently_ignored(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "missing.toml")


def test_history_reconciliation_notifies_only_unseen_reports(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    service = Service(processor.config, processor, store)
    report = _quake("report-1", "DetailScale", scale=40)

    async def history() -> list[dict[str, Any]]:
        return [report]

    monkeypatch.setattr("awoke.service.fetch_history", history)
    asyncio.run(service._reconcile_history(notify_new=True))
    asyncio.run(service._reconcile_history(notify_new=True))
    assert len(discord.sent) == 1
    store.close()
