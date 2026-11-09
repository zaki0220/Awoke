from __future__ import annotations

import csv
import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import shapefile
from PIL import Image, ImageDraw, ImageFont

LOG = logging.getLogger(__name__)
SCALE_LABELS_TEXT = {
    10: "震度1", 20: "震度2", 30: "震度3", 40: "震度4", 45: "震度5弱",
    50: "震度5強", 55: "震度6弱", 60: "震度6強", 70: "震度7",
}
SCALE_RGB = {
    10: (242, 242, 255), 20: (0, 170, 255), 30: (0, 65, 255),
    40: (250, 230, 150), 45: (255, 230, 0), 50: (255, 153, 0),
    55: (55, 40, 0), 60: (165, 0, 33), 70: (180, 0, 104),
}


class MapRenderer:
    def __init__(self, mapping_path: Path, seismic_path: Path, tsunami_path: Path, land_path: Path) -> None:
        self.mapping_path = mapping_path
        self.seismic_path = seismic_path
        self.tsunami_path = tsunami_path
        self.land_path = land_path

    def render(self, code: int, event: dict[str, Any]) -> bytes | None:
        try:
            return self._render(code, event)
        except (OSError, ValueError, shapefile.ShapefileException, KeyError) as error:
            LOG.exception("Map rendering failed: %s", error)
            return None

    def _render(self, code: int, event: dict[str, Any]) -> bytes:
        boundary_path = self.tsunami_path if code == 552 else self.seismic_path
        shapes, records, fields = self._read_shapes(boundary_path)
        field_names = [field[0] for field in fields]
        name_field = next((name for name in ("name", "NAME") if name in field_names), None)
        code_field = next((name for name in ("code", "CODE") if name in field_names), None)
        if name_field is None or code_field is None:
            raise ValueError(f"GIS fields missing name/code: {field_names}")
        by_name = {str(record[name_field]): str(record[code_field]) for record in records}
        by_code = {str(record[code_field]): shape for shape, record in zip(shapes, records)}
        fills = self._fills(code, event, by_name)
        selected = [(by_code[key], color) for key, color in fills.items() if key in by_code]
        if not selected:
            raise ValueError("No matching regional boundary for this event")
        points = [point for shape, _ in selected for point in shape.points]
        hypocenter = event.get("hypocenter") or {}
        lon, lat = _valid_coords(hypocenter.get("longitude"), hypocenter.get("latitude"))
        if lon is not None:
            points.append((lon, lat))
        min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
        min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
        dx, dy = max(max_x - min_x, 0.4), max(max_y - min_y, 0.4)
        margin_x, margin_y = max(dx * 0.18, 0.25), max(dy * 0.18, 0.25)
        bounds = min_x - margin_x, min_y - margin_y, max_x + margin_x, max_y + margin_y
        image = Image.new("RGB", (1100, 760), "#eaf4f8")
        draw = ImageDraw.Draw(image)
        self._draw_land(draw, bounds, image.size)
        for shape, color in selected:
            _draw_shape(draw, shape, bounds, image.size, color, outline=(60, 70, 80))
        if lon is not None:
            px, py = _project(lon, lat, bounds, image.size)
            draw.ellipse((px - 8, py - 8, px + 8, py + 8), fill=(200, 0, 0), outline="white", width=2)
        title = f"{event.get('title', '地震情報')}  {event.get('max_scale_text') or ''}".strip()
        draw.text((20, 18), title, fill=(20, 30, 40), font=_font(24))
        if code in (551, 556):
            self._draw_scale_legend(draw)
        footer_font = _font(12)
        draw.text(
            (20, 701),
            "出典：気象庁 予報区等GISデータを加工して作成 / Made with Natural Earth.",
            fill=(30, 40, 50),
            font=footer_font,
        )
        draw.text((20, 728), "https://www.data.jma.go.jp/developer/gis.html", fill=(30, 40, 50), font=footer_font)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def _fills(self, code: int, event: dict[str, Any], by_name: dict[str, str]) -> dict[str, tuple[int, int, int]]:
        result: dict[str, tuple[int, int, int]] = {}
        if code == 552:
            for area in event.get("areas", []):
                name = area.get("name")
                if name in by_name:
                    result[by_name[name]] = (255, 153, 0)
            return result

        if code == 556:
            for area in event.get("areas", []):
                name = area.get("name")
                scale = area.get("scaleTo")
                if name in by_name and scale in SCALE_RGB:
                    result[by_name[name]] = SCALE_RGB[scale]
            return result
        mapping: dict[tuple[str, str], str] = {}
        with self.mapping_path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row["code"] == "551" and row["status"] == "mapped":
                    mapping[(row["prefecture"], row["source_name"])] = row["boundary_code"]
        scales: dict[str, int] = {}
        for point in event.get("points", []):
            scale = point.get("scale")
            if not isinstance(scale, int) or scale not in SCALE_RGB:
                continue
            if point.get("isArea"):
                boundary = by_name.get(point.get("addr", ""))
            else:
                boundary = mapping.get((point.get("pref", ""), point.get("addr", "")))
            if boundary:
                scales[boundary] = max(scale, scales.get(boundary, 0))
        for boundary, scale in scales.items():
            result[boundary] = SCALE_RGB[scale]
        return result

    @staticmethod
    def _draw_scale_legend(draw: ImageDraw.ImageDraw) -> None:
        font = _font(14)
        draw.rectangle((18, 54, 350, 151), fill=(255, 255, 255), outline=(110, 120, 130), width=1)
        for index, (scale, color) in enumerate(SCALE_RGB.items()):
            column, row = index % 3, index // 3
            x, y = 28 + column * 106, 63 + row * 28
            draw.rectangle((x, y + 2, x + 17, y + 18), fill=color, outline=(80, 80, 80))
            draw.text((x + 22, y), SCALE_LABELS_TEXT[scale], fill=(20, 25, 30), font=font)

    def _draw_land(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[float, float, float, float],
        size: tuple[int, int],
    ) -> None:
        if not self.land_path.exists():
            return
        shapes, _, _ = self._read_shapes(self.land_path)
        for shape in shapes:
            sx, sy, ex, ey = shape.bbox
            if ex < bounds[0] or sx > bounds[2] or ey < bounds[1] or sy > bounds[3]:
                continue
            _draw_shape(draw, shape, bounds, size, (225, 225, 215), outline=(190, 190, 180))

    @staticmethod
    @lru_cache(maxsize=3)
    def _read_shapes(path: Path) -> tuple[list[Any], list[dict[str, Any]], list[tuple[str, str, int, int]]]:
        reader = shapefile.Reader(str(path), encoding="utf-8")
        fields = reader.fields[1:]
        field_names = [field[0] for field in fields]
        records = [dict(zip(field_names, values)) for values in reader.records()]
        return list(reader.shapes()), records, fields


def _valid_coords(lon: Any, lat: Any) -> tuple[float | None, float | None]:
    try:
        longitude, latitude = float(lon), float(lat)
    except (TypeError, ValueError):
        return None, None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90) or longitude == -200:
        return None, None
    return longitude, latitude


def _project(x: float, y: float, bounds: tuple[float, float, float, float], size: tuple[int, int]) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = bounds
    width, height = size
    scale = min((width - 40) / (max_x - min_x), (height - 90) / (max_y - min_y))
    offset_x = (width - (max_x - min_x) * scale) / 2
    offset_y = 40 + (height - 90 - (max_y - min_y) * scale) / 2
    return offset_x + (x - min_x) * scale, offset_y + (max_y - y) * scale


def _draw_shape(
    draw: ImageDraw.ImageDraw,
    shape: Any,
    bounds: tuple[float, float, float, float],
    size: tuple[int, int],
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
) -> None:
    indices = list(shape.parts) + [len(shape.points)]
    for start, end in zip(indices, indices[1:]):
        ring = [_project(x, y, bounds, size) for x, y in shape.points[start:end]]
        if len(ring) >= 3:
            draw.polygon(ring, fill=fill, outline=outline)


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("C:/Windows/Fonts/meiryo.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()
