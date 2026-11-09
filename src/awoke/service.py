from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from awoke.config import Config, webhook_credential
from awoke.discord import DiscordClient
from awoke.map_render import MapRenderer
from awoke.p2p import fetch_history, open_stream, stream_reader
from awoke.store import Store

LOG = logging.getLogger("awoke")
SCALE_LABELS = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7"}
JST = timezone(timedelta(hours=9))


class EventProcessor:
    def __init__(self, config: Config, store: Store, discord: DiscordClient, renderer: MapRenderer) -> None:
        self.config = config
        self.store = store
        self.discord = discord
        self.renderer = renderer

    async def process(self, payload: dict[str, Any], notify: bool = True) -> None:
        if self.config.test_mode and "id" not in payload:
            sandbox_id = payload.get("_id")
            if isinstance(sandbox_id, str) and sandbox_id:
                payload = {**payload, "id": sandbox_id}
        try:
            code = int(payload.get("code", 0))
            report_id = str(payload["id"])
        except (KeyError, TypeError, ValueError):
            LOG.warning("Ignoring P2P payload without a valid code or id")
            return
        if code not in (551, 552, 556):
            return
        if not self.config.test_mode and _is_expired_report(code, payload, self.config.retention_days):
            LOG.info("Ignoring report older than retention window: code=%s id=%s", code, report_id)
            return
        if self.store.report_seen(code, report_id):
            event_key = self.store.report_event_key(code, report_id)
            if event_key is None:
                LOG.warning("Previously received report has no event association: code=%s id=%s", code, report_id)
                return
            current = self.store.get_event(event_key)
            if current is None:
                LOG.warning("Previously received report points to expired event: code=%s id=%s", code, report_id)
                return
            if code == 556 and _is_older_serial(payload, current.get("last_serial")):
                LOG.info("Ignoring duplicate out-of-order EEW serial: eventId=%s", event_key)
                return
            state = self._apply(code, current["state"], payload)
            self.store.save_event(event_key, code, _event_time(code, payload), state, current["message_id"])
            self.store.mark_report(code, report_id, payload, event_key)
            await self._publish(code, event_key, state, current["message_id"], notify)
            return

        if code == 551:
            event_key = self._match_551(payload)
        elif code == 556:
            event_id = (payload.get("issue") or {}).get("eventId")
            event_key = f"556:{event_id}" if event_id else None
            if event_key is None:
                LOG.warning("EEW report has no issue.eventId; leaving unassociated: id=%s", report_id)
                return
        else:
            event_key = f"552:{report_id}"

        current = self.store.get_event(event_key) if event_key else None
        if code == 556 and current:
            serial = _int_or_none((payload.get("issue") or {}).get("serial"))
            old_serial = current.get("last_serial")
            if serial is not None and old_serial is not None and serial < old_serial:
                self.store.mark_report(code, report_id, payload, event_key)
                LOG.info("Ignoring out-of-order EEW serial: eventId=%s serial=%s latest=%s", event_key, serial, old_serial)
                return

        old_state = current["state"] if current else {}
        state = self._apply(code, old_state, payload)
        message_id = current["message_id"] if current else None
        serial = _int_or_none((payload.get("issue") or {}).get("serial")) if code == 556 else None
        self.store.save_event(event_key, code, _event_time(code, payload), state, message_id, serial)
        self.store.mark_report(code, report_id, payload, event_key)
        await self._publish(code, event_key, state, message_id, notify)

    def _match_551(self, payload: dict[str, Any]) -> str:
        event_time = _event_time(551, payload)
        candidates = self.store.events_for_time(551, event_time) if event_time else []
        incoming_coords = _coords(payload)
        compatible: list[dict[str, Any]] = []
        for candidate in candidates:
            old_coords = _coords_from_state(candidate["state"])
            if incoming_coords and old_coords:
                if _distance_km(incoming_coords, old_coords) <= self.config.event_match_radius_km:
                    compatible.append(candidate)
            else:
                compatible.append(candidate)
        if len(compatible) == 1:
            return compatible[0]["event_key"]
        if len(compatible) > 1:
            LOG.warning(
                "Ambiguous 551 event match; creating separate state: event_time=%s candidates=%s",
                event_time,
                len(compatible),
            )
        return self.store.new_event_key(551, event_time)

    @staticmethod
    def _apply(code: int, previous: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(previous)
        state["code"] = code
        state["report_id"] = payload.get("id")
        state["received_time"] = payload.get("time") or state.get("received_time")
        if "cancelled" in payload:
            state["cancelled"] = bool(payload["cancelled"])
        else:
            state.setdefault("cancelled", False)
        if code == 551:
            earthquake = payload.get("earthquake") or {}
            hypocenter = earthquake.get("hypocenter") or {}
            state["event_time"] = earthquake.get("time") or state.get("event_time")
            current_hypocenter = dict(state.get("hypocenter") or {})
            coords = _coords(payload)
            if coords:
                current_hypocenter.update(longitude=coords[0], latitude=coords[1])
            if hypocenter.get("name"):
                current_hypocenter["name"] = hypocenter["name"]
            for key, invalid in (("depth", -1), ("magnitude", -1)):
                value = _valid_measure(hypocenter.get(key), invalid)
                if value is not None:
                    current_hypocenter[key] = value
            if current_hypocenter:
                state["hypocenter"] = current_hypocenter
            if isinstance(earthquake.get("maxScale"), int) and earthquake["maxScale"] in SCALE_LABELS:
                state["max_scale"] = earthquake["maxScale"]
                state["max_scale_text"] = SCALE_LABELS.get(earthquake["maxScale"])
            points = payload.get("points")
            if isinstance(points, list) and points:
                state["points"] = points
            issue = payload.get("issue") or {}
            state["issue_type"] = issue.get("type")
            state["issue_correct"] = issue.get("correct")
            state["issue_time"] = issue.get("time")
            state["title"] = "地震情報"
        elif code == 552:
            areas = payload.get("areas")
            if isinstance(areas, list) and areas:
                state["areas"] = areas
            issue = payload.get("issue") or {}
            if issue.get("type"):
                state["issue_type"] = issue["type"]
            state["title"] = "津波情報"
        else:
            areas = payload.get("areas")
            if isinstance(areas, list) and areas:
                state["areas"] = areas
            issue = payload.get("issue") or {}
            if issue:
                state["issue"] = {**(state.get("issue") or {}), **issue}
            earthquake = payload.get("earthquake") or {}
            if earthquake:
                merged_earthquake = {**(state.get("earthquake") or {}), **earthquake}
                if isinstance(earthquake.get("hypocenter"), dict):
                    merged_earthquake["hypocenter"] = {
                        **((state.get("earthquake") or {}).get("hypocenter") or {}),
                        **earthquake["hypocenter"],
                    }
                state["earthquake"] = merged_earthquake
            state["title"] = "緊急地震速報（警報）"
        return state

    async def _publish(
        self,
        code: int,
        event_key: str,
        state: dict[str, Any],
        message_id: str | None,
        notify: bool,
    ) -> None:
        if not notify:
            if message_id:
                await self._edit(code, event_key, state, message_id)
            return
        if code == 551 and _int_or_none(state.get("max_scale")) is None:
            LOG.warning("551 report has no valid maximum intensity; not notifying event=%s", event_key)
            return
        should_send = code != 551 or state.get("max_scale", -1) >= self.config.threshold
        if not message_id and not should_send:
            return
        content, image = self._notification(code, state)
        if message_id:
            await self._edit(code, event_key, state, message_id, content, image)
            return
        try:
            created_id = await self.discord.send(content, image)
        except Exception:
            LOG.exception("Discord send failed; not retrying ambiguous outcome event=%s", event_key)
            return
        if created_id:
            self.store.set_message_id(event_key, created_id)

    async def _edit(
        self,
        code: int,
        event_key: str,
        state: dict[str, Any],
        message_id: str,
        content: str | None = None,
        image: bytes | None = None,
    ) -> None:
        if content is None:
            content, image = self._notification(code, state)
        try:
            await self.discord.edit(message_id, content, image)
        except Exception:
            LOG.exception("Discord edit failed; posting replacement event=%s", event_key)
            try:
                new_id = await self.discord.send(content, image)
                if new_id:
                    self.store.set_message_id(event_key, new_id)
            except Exception:
                LOG.exception("Discord replacement post failed event=%s", event_key)

    def _notification(self, code: int, state: dict[str, Any]) -> tuple[str, bytes | None]:
        content = _format_message(code, state)
        map_state = state
        if self.config.test_mode:
            content = "**【テスト】過去のサンドボックスデータです。実際の速報ではありません。**\n" + content
            map_state = {**state, "title": f"【テスト】{state.get('title', '')}"}
        return content, self.renderer.render(code, map_state)


class Service:
    def __init__(self, config: Config, processor: EventProcessor, store: Store) -> None:
        self.config = config
        self.processor = processor
        self.store = store
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self.initialized = False

    async def run(self) -> None:
        delay = self.config.reconnect_min_seconds
        while True:
            try:
                async with await open_stream(self.config.test_mode) as socket:
                    LOG.info("Connected to P2P WebSocket mode=%s", "test" if self.config.test_mode else "production")
                    reader = asyncio.create_task(stream_reader(socket, self.queue))
                    try:
                        if not self.initialized:
                            await self._initialize()
                            self.initialized = True
                        elif not self.config.test_mode:
                            await self._reconcile_history(notify_new=True)
                        delay = self.config.reconnect_min_seconds
                        await self._consume_until_reader_ends(reader)
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("P2P connection failed")
            wait = min(delay, self.config.reconnect_max_seconds) * random.uniform(0.8, 1.2)
            LOG.warning("Reconnecting to P2P in %.1f seconds", wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, self.config.reconnect_max_seconds)

    async def _consume_until_reader_ends(self, reader: asyncio.Task[None]) -> None:
        while not reader.done():
            try:
                payload = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self.processor.process(payload)
        await reader

    async def _initialize(self) -> None:
        if self.config.test_mode:
            LOG.warning("Test mode: sandbox data will be notified; history reconciliation is disabled")
            return
        known_ids = await self._reconcile_history()
        buffered: list[dict[str, Any]] = []
        while True:
            try:
                buffered.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for payload in buffered:
            marker = (int(payload.get("code", 0)), str(payload.get("id", "")))
            if marker in known_ids and self.store.report_payload(*marker) == payload:
                continue
            await self.processor.process(payload)
        LOG.info("Startup baseline initialized; discarded %s historical frames", len(buffered) - sum(
            (int(row.get("code", 0)), str(row.get("id", ""))) not in known_ids for row in buffered
        ))

    async def _reconcile_history(self, notify_new: bool = False) -> set[tuple[int, str]]:
        history = await fetch_history()
        for item in reversed(history):
            code, report_id = int(item.get("code", 0)), str(item.get("id", ""))
            is_new = not self.store.report_seen(code, report_id)
            await self.processor.process(item, notify=notify_new and is_new)
        LOG.info("Loaded %s reports from P2P history as a silent baseline", len(history))
        return {
            (int(item["code"]), str(item["id"]))
            for item in history
            if item.get("code") is not None and item.get("id") is not None
        }

    async def maintenance(self) -> None:
        while True:
            await asyncio.sleep(3600)
            removed = self.store.prune(self.config.retention_days)
            if removed:
                LOG.info("Expired %s event records", removed)


async def _run(config_path: Path) -> None:
    config = Config.load(config_path)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    credential = webhook_credential()
    if config.test_mode:
        LOG.warning("Test mode enabled: past sandbox data will be sent to the configured Discord webhook")
    store = Store(config.effective_database)
    discord = DiscordClient(credential)
    renderer = MapRenderer(config.area_mapping, config.seismic_shapefile, config.tsunami_shapefile, config.land_shapefile)
    service = Service(config, EventProcessor(config, store, discord, renderer), store)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runner = asyncio.create_task(service.run())
    maintenance = asyncio.create_task(service.maintenance())
    try:
        await stop.wait()
    finally:
        runner.cancel()
        maintenance.cancel()
        await asyncio.gather(runner, maintenance, return_exceptions=True)
        store.close()
        LOG.info("Service stopped cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="P2P earthquake alerts to Discord")
    parser.add_argument("--config", type=Path, default=Path("/etc/awoke/config.toml"))
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.config))
    except Exception:
        logging.getLogger("awoke").exception("Service startup failed")
        raise SystemExit(1)


def _event_time(code: int, payload: dict[str, Any]) -> str | None:
    if code == 551:
        return (payload.get("earthquake") or {}).get("time")
    if code == 556:
        return (payload.get("earthquake") or {}).get("originTime")
    return (payload.get("issue") or {}).get("time")


def _is_expired_report(code: int, payload: dict[str, Any], retention_days: int) -> bool:
    event_time = _event_time(code, payload)
    if not event_time:
        return False
    try:
        occurred = datetime.strptime(event_time, "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
    except ValueError:
        return False
    age = datetime.now(JST) - occurred
    return age.total_seconds() > retention_days * 86400


def _coords(payload: dict[str, Any]) -> tuple[float, float] | None:
    if int(payload.get("code", 0)) == 551:
        hypocenter = (payload.get("earthquake") or {}).get("hypocenter") or {}
    else:
        hypocenter = ((payload.get("earthquake") or {}).get("hypocenter") or {})
    try:
        lon, lat = float(hypocenter.get("longitude")), float(hypocenter.get("latitude"))
    except (TypeError, ValueError):
        return None
    if lon == -200 or lat == -200 or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def _coords_from_state(state: dict[str, Any]) -> tuple[float, float] | None:
    hypocenter = state.get("hypocenter") or {}
    return _coords({"code": 551, "earthquake": {"hypocenter": hypocenter}})


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(haversine))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_older_serial(payload: dict[str, Any], current_serial: int | None) -> bool:
    serial = _int_or_none((payload.get("issue") or {}).get("serial"))
    return serial is not None and current_serial is not None and serial < current_serial


def _valid_measure(value: Any, invalid: int) -> Any:
    return value if value is not None and value != invalid else None


def _format_event_time(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        LOG.warning("Unexpected earthquake event time format; displaying original value")
        return value
    return parsed.strftime("%Y/%m/%d %H:%M") + "頃"


def _format_message(code: int, state: dict[str, Any]) -> str:
    if code == 551:
        lines = ["**地震情報（観測）**"]
        hypocenter = state.get("hypocenter") or {}
        if state.get("event_time"):
            lines.append(f"発生時刻: {_format_event_time(state['event_time'])}")
        if hypocenter.get("name"):
            lines.append(f"震源: {hypocenter['name']}")
        if state.get("max_scale") in SCALE_LABELS:
            lines.append(f"最大震度: {SCALE_LABELS[state['max_scale']]}")
        if hypocenter.get("magnitude") is not None:
            lines.append(f"マグニチュード: M{hypocenter['magnitude']}")
        if hypocenter.get("depth") is not None:
            lines.append(f"深さ: {hypocenter['depth']} km")
        return "\n".join(lines)
    if code == 552:
        lines = ["**津波情報**"]
        if state.get("cancelled"):
            lines.append("津波警報・注意報が解除されました。")
        else:
            for area in state.get("areas", []):
                bits = [area.get("name", "")]
                if area.get("grade"):
                    grade = {
                        "Watch": "津波注意報",
                        "Warning": "津波警報",
                        "MajorWarning": "大津波警報",
                    }.get(str(area["grade"]), str(area["grade"]))
                    bits.append(grade)
                height = (area.get("maxHeight") or {}).get("description")
                if height:
                    bits.append(f"予想高さ {height}")
                arrival = (area.get("firstHeight") or {}).get("condition")
                if arrival:
                    bits.append(str(arrival))
                lines.append(" / ".join(bits))
        return "\n".join(lines)
    issue = state.get("issue") or {}
    earthquake = state.get("earthquake") or {}
    hypocenter = earthquake.get("hypocenter") or {}
    lines = ["**緊急地震速報（警報・予測）**"]
    if state.get("cancelled"):
        lines.append("取消報")
    if issue.get("eventId"):
        lines.append(f"イベント: {issue['eventId']} / 第{issue.get('serial', '?')}報")
    if hypocenter.get("name"):
        lines.append(f"震源: {hypocenter['name']}")
    if hypocenter.get("magnitude") is not None and hypocenter.get("magnitude") >= 0:
        lines.append(f"マグニチュード: M{hypocenter['magnitude']}")
    for area in state.get("areas", []):
        scale = SCALE_LABELS.get(area.get("scaleTo"), "不明")
        lines.append(f"{area.get('name', '')}: 予想震度 {scale}")
    return "\n".join(lines)
