from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import shapefile
from PIL import Image, ImageDraw

from awoke import map_render
from awoke.config import Config
from awoke.map_render import MapRenderer
from awoke.service import EventProcessor, Service, _distance_km, _format_message, _is_expired_report
from awoke.store import Store

_JST = timezone(timedelta(hours=9))
_TEST_EVENT_TIME = datetime.now(_JST).strftime("%Y/%m/%d %H:%M:%S")


class FakeDiscord:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_images: list[bytes | None] = []
        self.edited: list[tuple[str, str]] = []
        self.content_edits: list[tuple[str, str]] = []

    async def send(self, content: str, image: bytes | None = None) -> str:
        self.sent.append(content)
        self.sent_images.append(image)
        return f"message-{len(self.sent)}"

    async def edit(self, message_id: str, content: str, image: bytes | None = None) -> None:
        self.edited.append((message_id, content))

    async def edit_content(self, message_id: str, content: str) -> None:
        self.content_edits.append((message_id, content))


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


def test_551_all_unmapped_still_sends_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("code,status,prefecture,source_name,boundary_code\n", encoding="utf-8")
    renderer = MapRenderer(mapping, tmp_path / "seismic.shp", tmp_path / "tsunami.shp", tmp_path / "land.shp")
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: ({"区域": "1"}, {"1": (0, "区域")}))
    processor.renderer = renderer
    report = _quake("unmapped", "DetailScale", scale=45)
    report["points"] = [
        {"isArea": False, "pref": "鹿児島県", "addr": "鹿児島十島村中之島徳之尾", "scale": 45},
    ]
    asyncio.run(processor.process(report))
    assert len(discord.sent) == 1
    assert "最大震度: 5弱" in discord.sent[0]
    assert discord.sent_images == [None]
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


def test_eew_update_edits_maximum_and_cancellation_keeps_notice(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    report = {
        "code": 556, "id": "eew-update",
        "issue": {"eventId": "event-update", "serial": 1},
        "earthquake": {"hypocenter": {"name": "茨城県沖", "magnitude": 5.2}},
        "areas": [{"name": "茨城県南部", "scaleTo": 40}],
    }
    asyncio.run(processor.process(report))
    assert "予想最大震度: 4" in discord.sent[0]
    update = dict(report, issue={"eventId": "event-update", "serial": 2},
                  areas=[{"name": "栃木県南部", "scaleTo": 50}])
    asyncio.run(processor.process(update))
    assert "予想最大震度: 5強" in discord.edited[-1][1]
    assert "栃木県南部" not in discord.edited[-1][1]
    asyncio.run(processor.process(dict(update, issue={"eventId": "event-update", "serial": 3},
                                       cancelled=True, areas=[])))
    assert "取消報" in discord.edited[-1][1]
    store.close()


def test_eew_unknown_forecast_sends_red_map_with_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    seismic = tmp_path / "seismic.shp"
    with shapefile.Writer(str(seismic), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        writer.field("name", "C", size=40)
        writer.field("code", "C", size=10)
        writer.poly([[(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75), (0.25, 0.25)]])
        writer.record("茨城県南部", "221")
    processor.renderer = MapRenderer(tmp_path / "mapping.csv", seismic, tmp_path / "tsunami.shp", tmp_path / "land.shp")
    monkeypatch.setattr(map_render, "MAP_SIZE", (200, 200))
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(processor.renderer, "_region_column_width", lambda groups: 200)
    monkeypatch.setattr(processor.renderer, "_draw_region_labels", lambda *args: None)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *args, **kwargs: None)
    report = {
        "code": 556, "id": "eew-unknown",
        "issue": {"eventId": "unknown-event", "serial": "1"},
        "areas": [{"name": "茨城県南部", "scaleTo": -1}],
    }
    asyncio.run(processor.process(report))
    assert "予想最大震度: 不明" in discord.sent[0]
    assert "茨城県南部" not in discord.sent[0]
    assert discord.sent_images[0] is not None
    with Image.open(io.BytesIO(discord.sent_images[0])) as image:
        assert image.size == map_render.IMAGE_SIZE
        colors = {color for _, color in image.getcolors(image.width * image.height)}
        assert map_render.EEW_WARNING_COLOR in colors
    store.close()


@pytest.mark.parametrize("areas,expected", [
    ([{"scaleTo": 40}, {"scaleTo": 45.0}, {"scaleTo": 30}], "5弱"),
    ([{"scaleTo": 0.0}], "0"),
    ([{"scaleTo": 99, "scaleFrom": 50}], "5強以上"),
    ([{"scaleTo": 99, "scaleFrom": 45}, {"scaleTo": 55}], "6弱以上"),
    ([{"scaleTo": 40}, {"scaleTo": -1}], "4（一部地域の予想震度は不明）"),
    ([{"scaleTo": 40}, {"scaleTo": 99}], "4（一部地域の予想震度は不明）"),
    ([{"scaleTo": 50}, {"scaleTo": float("inf")}, {"scaleTo": True}], "5強（一部地域の予想震度は不明）"),
    ([{"scaleTo": -1}, {}], "不明"),
    ([], "不明"),
])
def test_eew_message_maximum_forecast_and_unknown_values(
    areas: list[dict[str, Any]], expected: str,
) -> None:
    state = {
        "issue": {"eventId": "123", "serial": 2},
        "earthquake": {"hypocenter": {"name": "震源地", "magnitude": 5.5}},
        "areas": [{"name": f"発表地域{i}", **area} for i, area in enumerate(areas)],
    }
    message = _format_message(556, state)
    assert "震源: 震源地" in message
    assert "マグニチュード: M5.5" in message
    assert f"予想最大震度: {expected}" in message
    assert "発表地域" not in message


def test_eew_sample_message_uses_maximum_forecast_not_region_list() -> None:
    sample = Path(__file__).parents[1] / "sample" / "p2p_sample_556.json"
    payload = json.loads(sample.read_text(encoding="utf-8-sig"))[0]
    state = EventProcessor._apply(556, {}, payload)
    message = _format_message(556, state)
    assert "震源: 愛知県東部" in message
    assert "マグニチュード: M5.5" in message
    assert "予想最大震度: 5弱" in message
    assert "愛知県西部" not in message
    assert "静岡県西部" not in message


def _tsunami(report_id: str, minute: int, areas: list[dict[str, Any]], cancelled: bool = False) -> dict[str, Any]:
    return {
        "code": 552, "id": report_id, "cancelled": cancelled,
        "issue": {"type": "Focus", "source": "気象庁", "time": f"2026/11/10 12:{minute:02d}:00"},
        "areas": areas,
    }


def test_tsunami_direct_cancellation_preserves_peak_and_starts_new_cycle(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)
    base = _tsunami("one", 0, [{"name": "茨城県", "grade": "Watch"}])
    asyncio.run(processor.process(base))
    asyncio.run(processor.process(_tsunami("two", 1, [{"name": "茨城県", "grade": "Warning"}])))
    asyncio.run(processor.process(_tsunami("cancel", 2, [], True)))
    assert len(discord.sent) == 2
    assert discord.edited[0][0] == "message-1"
    assert "津波警報" in discord.content_edits[0][1]
    assert "津波情報（解除済）" in discord.content_edits[0][1]
    assert discord.content_edits[0][0] == "message-1"
    assert "解除されました" in discord.sent[1]
    asyncio.run(processor.process(_tsunami("old", 1, base["areas"])))
    assert len(discord.sent) == 2
    asyncio.run(processor.process(_tsunami("new", 3, base["areas"])))
    assert len(discord.sent) == 3
    store.close()


def test_tsunami_shrink_and_downgrade_archive_all_peaks_across_restart(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)
    area = lambda name, grade: {"name": name, "grade": grade}
    reports = [
        _tsunami("a", 0, [area("A", "Warning"), area("B", "Watch")]),
        _tsunami("b", 1, [area("A", "MajorWarning"), area("B", "Watch")]),
        _tsunami("c", 2, [area("A", "Warning"), area("C", "Watch")]),
        _tsunami("d", 3, [area("A", "Watch"), area("C", "Watch")]),
        _tsunami("e", 4, [], True),
    ]
    for report in reports[:3]:
        asyncio.run(processor.process(report))
    store.close()
    store = Store(tmp_path / "state.sqlite3")
    processor = EventProcessor(processor.config, store, discord, FakeRenderer())
    for report in reports[3:]:
        asyncio.run(processor.process(report))
    assert len(discord.sent) == 3
    assert [item[0] for item in discord.edited] == ["message-1", "message-3"]
    assert [item[0] for item in discord.content_edits] == ["message-1", "message-2"]
    assert all("（解除済）" in content for _, content in discord.content_edits)
    assert "解除されました" in discord.edited[-1][1]
    state = store.get_event("552:timeline")["state"]
    assert state["cancelled"] and state["snapshot"]["areas"] == []
    assert all(item["marked"] for item in state["archives"])
    store.close()


def test_tsunami_order_duplicates_and_conflicts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)
    report = _tsunami("a", 1, [{"name": "A", "grade": "Watch"}])
    asyncio.run(processor.process(report))
    asyncio.run(processor.process(dict(report, id="duplicate", time="later")))
    asyncio.run(processor.process(_tsunami("conflict", 1, [{"name": "B", "grade": "Warning"}])))
    asyncio.run(processor.process(_tsunami("old", 0, [], True)))
    asyncio.run(processor.process(_tsunami("missing", 2, [])))
    asyncio.run(processor.process({**report, "id": "invalid", "issue": {}}))
    assert len(discord.sent) == 1
    assert not discord.edited
    assert "Conflicting tsunami reports" in caplog.text
    assert "invalid issue.time" in caplog.text
    assert store.get_event("552:timeline")["state"]["snapshot"]["areas"] == report["areas"]
    store.close()


def test_tsunami_sample_replayed_after_cancellation_does_not_reopen(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)
    sample_path = Path(__file__).parents[1] / "sample" / "p2p_sample_552.json"
    reports = json.loads(sample_path.read_text(encoding="utf-8-sig"))
    reports = [report for report in reports if report["issue"]["time"].startswith("2023/10/09")]
    reports.sort(key=lambda report: report["issue"]["time"])
    for report in reports:
        asyncio.run(processor.process(report))
    assert len(discord.sent) == 2
    assert len(discord.edited) == 4
    assert len(discord.content_edits) == 1
    assert store.get_event("552:timeline")["state"]["cancelled"]
    sent_count = len(discord.sent)
    for report in reports:
        asyncio.run(processor.process(report))
    assert len(discord.sent) == sent_count
    assert len(discord.edited) == 4
    store.close()


def test_tsunami_failed_archive_mark_is_retried_after_next_cycle(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)
    original = discord.edit_content

    async def failure(message_id: str, content: str) -> None:
        raise RuntimeError("offline")

    discord.edit_content = failure
    asyncio.run(processor.process(_tsunami("a", 0, [{"name": "A", "grade": "Watch"}])))
    asyncio.run(processor.process(_tsunami("b", 1, [], True)))
    asyncio.run(processor.process(_tsunami("c", 2, [{"name": "B", "grade": "Watch"}])))
    discord.edit_content = original
    asyncio.run(processor.process(_tsunami("d", 3, [{"name": "B", "grade": "Warning"}])))
    assert discord.content_edits[0][0] == "message-1"
    assert "（解除済）" in discord.content_edits[0][1]
    store.close()


def test_tsunami_failed_edit_keeps_old_post_for_cancellation_mark(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)

    async def failure(message_id: str, content: str, image: bytes | None = None) -> None:
        raise RuntimeError("offline")

    discord.edit = failure
    asyncio.run(processor.process(_tsunami("a", 0, [{"name": "A", "grade": "Watch"}])))
    asyncio.run(processor.process(_tsunami("b", 1, [{"name": "A", "grade": "Warning"}])))
    asyncio.run(processor.process(_tsunami("c", 2, [], True)))
    assert len(discord.sent) == 3
    assert [item[0] for item in discord.content_edits] == ["message-1", "message-2"]
    store.close()


def test_tsunami_silent_baseline_and_later_update(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    processor.config = replace(processor.config, test_mode=True)
    first = _tsunami("a", 0, [{"name": "A", "grade": "Watch"}])
    asyncio.run(processor.process(first, notify=False))
    assert not discord.sent
    asyncio.run(processor.process(_tsunami("b", 1, [{"name": "A", "grade": "Warning"}])))
    assert len(discord.sent) == 1
    asyncio.run(processor.process(_tsunami("c", 2, [], True)))
    assert len(discord.sent) == 2
    assert discord.content_edits[0][0] == "message-1"
    store.close()


def test_tsunami_message_groups_areas_by_severity_and_keeps_arrival_details() -> None:
    state = {"areas": [
        {"name": "千葉県", "grade": "Watch", "maxHeight": {"description": "１ｍ"},
         "firstHeight": {"condition": "津波到達中と推測"}},
        {"name": "宮城県", "grade": "MajorWarning", "maxHeight": {"description": "巨大"},
         "firstHeight": {"arrivalTime": "2023/10/09 12:30:45", "condition": "第１波の到達を確認"}},
        {"name": "高知県", "grade": "Warning", "maxHeight": {"description": "３ｍ"},
         "firstHeight": {"arrivalTime": "2023/10/09 13:00:00"}},
        {"name": "岩手県", "grade": "MajorWarning", "maxHeight": {"description": "１０ｍ超"}},
        {"name": "静岡県", "grade": "Watch"},
    ]}
    assert _format_message(552, state) == (
        "**津波情報**\n"
        "【大津波警報】\n"
        "宮城県\t巨大\t12:30 / 第１波の到達を確認\n"
        "岩手県\t１０ｍ超\t不明\n"
        "【津波警報】\n"
        "高知県\t３ｍ\t13:00\n"
        "【津波注意報】\n"
        "千葉県\t１ｍ\t津波到達中と推測\n"
        "静岡県\t不明\t不明"
    )


def test_tsunami_arrival_time_keeps_invalid_values_and_issue_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = {
        "issue_time": "2023/10/09 23:59:30",
        "areas": [
            {"name": "区域A", "grade": "Watch", "firstHeight": {"arrivalTime": "2023/10/10 00:01:59"}},
            {"name": "区域B", "grade": "Watch", "firstHeight": {"arrivalTime": "時刻未定", "condition": "到達中"}},
        ],
    }
    assert _format_message(552, state) == (
        "**津波情報**\n"
        "発表時刻: 2023/10/09 23:59:30\n"
        "【津波注意報】\n"
        "区域A\t不明\t00:01\n"
        "区域B\t不明\t時刻未定 / 到達中"
    )
    assert "Unexpected tsunami arrival time format" in caplog.text


def test_tsunami_message_preserves_unknown_grades(caplog: pytest.LogCaptureFixture) -> None:
    state = {"areas": [
        {"name": "区域A", "grade": "Unknown"},
        {"name": "区域B", "grade": "Unexpected"},
        {"name": "区域C"},
    ]}
    assert _format_message(552, state) == (
        "**津波情報**\n"
        "【種別不明】\n区域A\t不明\t不明\n区域C\t不明\t不明\n"
        "【種別不明（Unexpected）】\n区域B\t不明\t不明"
    )
    assert "Unknown tsunami grade in notification: grade=Unexpected" in caplog.text


def test_unknown_intensity_is_not_formatted_as_zero() -> None:
    message = _format_message(551, {"event_time": _TEST_EVENT_TIME, "max_scale": None})
    assert "最大震度" not in message


@pytest.mark.parametrize("code,state", [
    (551, {"hypocenter": {"magnitude": 4.4}}),
    (552, {"areas": []}),
    (556, {"earthquake": {"hypocenter": {"magnitude": 4.4}}}),
])
def test_message_source_removed_and_magnitude_formatted(code: int, state: dict[str, Any]) -> None:
    message = _format_message(code, state)
    assert "情報提供: P2P地震情報" not in message
    if code != 552:
        assert "マグニチュード: M4.4" in message


def test_earthquake_message_displays_minute_and_omits_observation_count() -> None:
    state = {"event_time": "2023/05/11 18:52:34", "max_scale": 40, "points": [{"scale": 40}]}
    message = _format_message(551, state)
    assert "発生時刻: 2023/05/11 18:52頃" in message
    assert "18:52:34" not in message
    assert "観測地域・地点数" not in message
    assert "最大震度: 4" in message


def test_unexpected_event_time_is_preserved_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    message = _format_message(551, {"event_time": "発生時刻不明"})
    assert "発生時刻: 発生時刻不明" in message
    assert "Unexpected earthquake event time format" in caplog.text


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


@pytest.mark.parametrize("value", ['"true"', "1"])
def test_test_mode_requires_boolean(tmp_path: Path, value: str) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[awoke]\ntest_mode = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="test_mode must be a boolean"):
        Config.load(config_path)


def test_test_mode_database_is_separate_from_production(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    production = Config(database=database)
    test_config = Config(database=database, test_mode=True)
    assert production.effective_database == database
    assert test_config.effective_database == tmp_path / "state.sqlite3.sandbox"
    production_store = Store(production.effective_database)
    sandbox_store = Store(test_config.effective_database)
    production_store.mark_report(551, "production-report", {}, "production-event")
    assert not sandbox_store.report_seen(551, "production-report")
    sandbox_store.mark_report(551, "sandbox-report", {}, "sandbox-event")
    assert not production_store.report_seen(551, "sandbox-report")
    sandbox_store.close()
    production_store.close()


def test_test_mode_notifies_old_reports_and_labels_new_and_edited_messages(tmp_path: Path) -> None:
    import asyncio

    class RecordingRenderer:
        def __init__(self) -> None:
            self.titles: list[str] = []

        def render(self, code: int, state: dict[str, Any]) -> None:
            self.titles.append(state["title"])
            return None

    config = Config(test_mode=True, database=tmp_path / "state.sqlite3")
    store = Store(config.effective_database)
    discord = FakeDiscord()
    renderer = RecordingRenderer()
    processor = EventProcessor(config, store, discord, renderer)
    report = _quake("sandbox-1", "DetailScale", scale=40)
    report["earthquake"]["time"] = "2023/04/01 00:00:00"
    asyncio.run(processor.process(report))
    asyncio.run(processor.process(report))
    assert len(discord.sent) == 1
    assert len(discord.edited) == 1
    assert "【テスト】" in discord.sent[0]
    assert "【テスト】" in discord.edited[0][1]
    assert all("【テスト】" in title for title in renderer.titles)
    store.close()


@pytest.mark.parametrize("test_mode", [False, True])
@pytest.mark.parametrize("code", [551, 552, 556])
def test_underscore_id_is_used_for_notifications_and_updates(
    tmp_path: Path, test_mode: bool, code: int,
) -> None:
    import asyncio

    config = Config(test_mode=test_mode, database=tmp_path / "state.sqlite3")
    store = Store(config.effective_database)
    discord = FakeDiscord()
    processor = EventProcessor(config, store, discord, FakeRenderer())
    if code == 551:
        report = _quake("report-1", "DetailScale", scale=40)
    elif code == 552:
        report = {
            "code": 552, "id": "report-1", "cancelled": False,
            "issue": {"time": _TEST_EVENT_TIME, "type": "Focus"},
            "areas": [{"name": "茨城県", "grade": "Watch"}],
        }
    else:
        report = {
            "code": 556, "id": "report-1", "issue": {"eventId": "event-1", "serial": 1},
            "areas": [{"name": "茨城県南部", "scaleTo": 40}],
        }
    report["_id"] = report.pop("id")
    asyncio.run(processor.process(report))
    asyncio.run(processor.process(report))
    assert store.report_seen(code, "report-1")
    assert len(discord.sent) == 1
    assert len(discord.edited) == (0 if code == 552 else 1)
    assert "id" not in report
    store.close()


@pytest.mark.parametrize("test_mode", [False, True])
@pytest.mark.parametrize("alternate_id", ["", None, 123])
def test_missing_or_invalid_alternate_id_is_not_processed(
    tmp_path: Path, test_mode: bool, alternate_id: str | int | None
) -> None:
    import asyncio

    config = Config(test_mode=test_mode, database=tmp_path / "state.sqlite3")
    store = Store(config.effective_database)
    discord = FakeDiscord()
    processor = EventProcessor(config, store, discord, FakeRenderer())
    report = _quake("sandbox-1", "DetailScale", scale=40)
    del report["id"]
    report["_id"] = alternate_id
    asyncio.run(processor.process(report))
    assert not discord.sent
    assert not store.report_seen(551, "sandbox-1")
    store.close()


def test_existing_id_takes_precedence_over_alternate(tmp_path: Path) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    report = _quake("primary", "DetailScale", scale=40)
    report["_id"] = "alternate"
    asyncio.run(processor.process(report))
    assert store.report_seen(551, "primary")
    assert not store.report_seen(551, "alternate")
    assert len(discord.sent) == 1
    store.close()


@pytest.mark.parametrize("history_field,buffer_field", [
    ("_id", "_id"), ("_id", "id"), ("id", "_id"),
])
def test_startup_baseline_deduplicates_alternate_id(
    tmp_path: Path, monkeypatch: Any, history_field: str, buffer_field: str,
) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    service = Service(processor.config, processor, store)
    report = _quake("report-1", "DetailScale", scale=40)
    history_report = dict(report)
    buffered_report = dict(report)
    if history_field == "_id":
        history_report["_id"] = history_report.pop("id")
    if buffer_field == "_id":
        buffered_report["_id"] = buffered_report.pop("id")

    async def history() -> list[dict[str, Any]]:
        return [history_report]

    monkeypatch.setattr("awoke.service.fetch_history", history)

    async def scenario() -> None:
        await service.queue.put(buffered_report)
        await service._initialize()

    asyncio.run(scenario())
    assert store.report_seen(551, "report-1")
    assert not discord.sent and not discord.edited
    store.close()


def test_reconnect_history_deduplicates_alternate_id(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    service = Service(processor.config, processor, store)
    report = _quake("report-1", "DetailScale", scale=40)
    report["_id"] = report.pop("id")

    async def history() -> list[dict[str, Any]]:
        return [report]

    monkeypatch.setattr("awoke.service.fetch_history", history)
    asyncio.run(service._reconcile_history(notify_new=True))
    asyncio.run(service._reconcile_history(notify_new=True))
    assert len(discord.sent) == 1
    assert store.report_seen(551, "report-1")
    store.close()


def test_startup_processes_changed_buffered_report_with_alternate_id(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    service = Service(processor.config, processor, store)
    initial = _quake("report-1", "DetailScale", scale=30)
    initial["_id"] = initial.pop("id")
    updated = _quake("report-1", "DetailScale", scale=40)

    async def history() -> list[dict[str, Any]]:
        return [initial]

    monkeypatch.setattr("awoke.service.fetch_history", history)

    async def scenario() -> None:
        await service.queue.put(updated)
        await service._initialize()

    asyncio.run(scenario())
    assert len(discord.sent) == 1
    assert "最大震度: 4" in discord.sent[0]
    store.close()


def test_history_invalid_id_is_logged_and_does_not_break_reconciliation(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    processor, store, discord = _processor(tmp_path)
    service = Service(processor.config, processor, store)
    valid = _quake("report-1", "DetailScale", scale=40)

    async def history() -> list[dict[str, Any]]:
        return [{"code": 551, "_id": ""}, valid]

    monkeypatch.setattr("awoke.service.fetch_history", history)
    markers = asyncio.run(service._reconcile_history(notify_new=True))
    assert markers == {(551, "report-1")}
    assert len(discord.sent) == 1
    assert "Ignoring P2P payload without a valid code or id" in caplog.text
    store.close()


def test_test_mode_startup_does_not_seed_history(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    config = Config(test_mode=True, database=tmp_path / "state.sqlite3")
    store = Store(config.effective_database)
    discord = FakeDiscord()
    service = Service(config, EventProcessor(config, store, discord, FakeRenderer()), store)
    report = _quake("sandbox-1", "DetailScale", scale=40)
    report["earthquake"]["time"] = "2023/04/01 00:00:00"

    async def fail_if_called() -> list[dict[str, Any]]:
        raise AssertionError("test mode must not fetch production history")

    monkeypatch.setattr("awoke.service.fetch_history", fail_if_called)

    async def scenario() -> None:
        await service.queue.put(report)
        await service._initialize()
        await service.processor.process(service.queue.get_nowait())

    asyncio.run(scenario())
    assert len(discord.sent) == 1
    store.close()


@pytest.mark.parametrize("test_mode", [False, True])
def test_websocket_endpoint_matches_mode(test_mode: bool, monkeypatch: Any) -> None:
    import asyncio

    from awoke import p2p

    urls: list[str] = []

    async def connect(url: str, **kwargs: Any) -> object:
        urls.append(url)
        return object()

    monkeypatch.setattr(p2p, "connect", connect)
    asyncio.run(p2p.open_stream(test_mode))
    assert urls == [p2p.SANDBOX_WS_URL if test_mode else p2p.WS_URL]


def test_sandbox_reconnect_processes_stream_without_history(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    config = Config(
        test_mode=True,
        database=tmp_path / "state.sqlite3",
        reconnect_min_seconds=0.01,
        reconnect_max_seconds=0.01,
    )
    store = Store(config.effective_database)
    modes: list[bool] = []

    class Socket:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    async def open_stream(test_mode: bool) -> Socket:
        modes.append(test_mode)
        return Socket()

    async def history() -> list[dict[str, Any]]:
        raise AssertionError("sandbox must not fetch production history")

    async def scenario() -> None:
        delivered = asyncio.Event()
        second_delivered = asyncio.Event()

        class SignallingDiscord(FakeDiscord):
            async def send(self, content: str, image: bytes | None = None) -> str:
                message_id = await super().send(content, image)
                if len(self.sent) == 1:
                    delivered.set()
                else:
                    second_delivered.set()
                return message_id

        discord = SignallingDiscord()
        service = Service(config, EventProcessor(config, store, discord, FakeRenderer()), store)

        async def reader(socket: object, queue: asyncio.Queue[dict[str, Any]]) -> None:
            report = _quake(f"sandbox-{len(modes)}", "DetailScale", scale=40)
            report["earthquake"]["time"] = f"2023/04/0{len(modes)} 00:00:00"
            await queue.put(report)
            if len(modes) == 1:
                await delivered.wait()
            else:
                await asyncio.Event().wait()

        monkeypatch.setattr("awoke.service.open_stream", open_stream)
        monkeypatch.setattr("awoke.service.stream_reader", reader)
        monkeypatch.setattr("awoke.service.fetch_history", history)
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(second_delivered.wait(), timeout=3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert modes == [True, True]
        assert len(discord.sent) == 2
        assert all("【テスト】" in content for content in discord.sent)

    try:
        asyncio.run(scenario())
    finally:
        store.close()
