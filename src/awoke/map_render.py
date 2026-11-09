from __future__ import annotations

import csv
import io
import logging
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import shapefile
from PIL import Image, ImageDraw, ImageFont

LOG = logging.getLogger(__name__)
SCALE_LABELS_TEXT = {
    10: "震度1", 20: "震度2", 30: "震度3", 40: "震度4", 45: "震度5弱",
    50: "震度5強", 55: "震度6弱", 60: "震度6強", 70: "震度7",
}
SCALE_RGB = {
    10: (70, 100, 110), 20: (30, 110, 230), 30: (0, 200, 200),
    40: (250, 250, 100), 45: (255, 180, 0), 50: (255, 120, 0),
    55: (230, 0, 0), 60: (160, 0, 0), 70: (150, 0, 150),
}
SCALE_MARKERS = {
    10: "1", 20: "2", 30: "3", 40: "4", 45: "5-", 50: "5+",
    55: "6-", 60: "6+", 70: "7",
}
MAP_SIZE = (2200, 1500)
PANEL_WIDTH = 650
REGION_COLUMNS = 3
LEGEND_HEIGHT = 240
SEA_COLOR = (23, 30, 37)
LAND_COLOR = (105, 111, 117)
BOUNDARY_COLOR = (225, 230, 235)
EEW_WARNING_COLOR = (255, 0, 0)
TSUNAMI_GRADES = {
    "Watch": ("津波注意報", (255, 255, 0), 1),
    "Warning": ("津波警報", (255, 0, 0), 2),
    "MajorWarning": ("大津波警報", (150, 0, 150), 3),
}
TSUNAMI_LINE_WIDTH = 6
TSUNAMI_GRADE_PRIORITY = {label: priority for label, _, priority in TSUNAMI_GRADES.values()}
MERCATOR_MAX_LATITUDE = 85.0511287798066


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

    def _render(self, code: int, event: dict[str, Any]) -> bytes | None:
        boundary_path = self.tsunami_path if code == 552 else self.seismic_path
        by_name, by_code = self._read_boundary_index(boundary_path)
        regions = self._regions(code, event, by_name)
        selected_indices = [
            (by_code[key][0], by_code[key][1], color, label)
            for key, (color, label) in regions.items() if key in by_code
        ]
        if not selected_indices:
            if code == 551 and not regions and any(
                not point.get("isArea")
                and isinstance(point.get("scale"), int)
                and point["scale"] in SCALE_RGB
                for point in event.get("points", [])
            ):
                LOG.warning("No mapped observation points for 551 report; sending without a map")
                return None
            raise ValueError("No matching regional boundary for this event")
        selected = self._read_selected_shapes(boundary_path, selected_indices)
        min_x = min(shape.bbox[0] for shape, _, _, _ in selected)
        min_y = min(shape.bbox[1] for shape, _, _, _ in selected)
        max_x = max(shape.bbox[2] for shape, _, _, _ in selected)
        max_y = max(shape.bbox[3] for shape, _, _, _ in selected)
        hypocenter = event.get("hypocenter") or (event.get("earthquake") or {}).get("hypocenter") or {}
        lon, lat = _valid_coords(hypocenter.get("longitude"), hypocenter.get("latitude"))
        if lon is not None:
            min_x, max_x = min(min_x, lon), max(max_x, lon)
            min_y, max_y = min(min_y, lat), max(max_y, lat)
        bounds = _expanded_bounds((min_x, min_y, max_x, max_y))
        map_size, top_padding = _map_layout(bounds, MAP_SIZE)
        projection = _Projection(bounds, map_size, top_padding)
        visible_bounds = projection.visible_bounds(map_size)
        groups = self._region_groups(selected, code)
        legend_height = LEGEND_HEIGHT if code == 551 else 0
        column_width = self._region_column_width(groups)
        panel_width = max(PANEL_WIDTH, 65 + REGION_COLUMNS * 200)
        list_width = max(panel_width, 65 + REGION_COLUMNS * column_width)
        list_height = 140 + sum(
            48 * (1 + math.ceil(len(items) / REGION_COLUMNS)) for _, _, items in groups
        )
        image = Image.new("RGB", (map_size[0] + panel_width, map_size[1] + 80), SEA_COLOR)
        map_image = Image.new("RGB", map_size, SEA_COLOR)
        draw = ImageDraw.Draw(map_image)
        self._draw_land(draw, visible_bounds, projection)
        if code == 552:
            for shape in self._iter_land_shapes(boundary_path, visible_bounds):
                _draw_boundary(draw, shape, projection)
        if code == 552:
            for shape, _, color, label in sorted(selected, key=lambda item: TSUNAMI_GRADE_PRIORITY[item[3]]):
                _draw_boundary(draw, shape, projection, color, TSUNAMI_LINE_WIDTH)
            self._draw_region_labels(draw, selected, projection)
        else:
            for shape, _, color, _ in selected:
                _draw_shape(draw, shape, projection, color, outline=BOUNDARY_COLOR)
            self._draw_intensity_markers(draw, selected, projection, code)
        if lon is not None:
            px, py = projection(lon, lat)
            _draw_hypocenter(draw, px, py)
        title = event.get("title", "地震情報")
        if event.get("max_scale_text"):
            title += f"  最大震度{event['max_scale_text']}"
        draw.rectangle((0, 0, map_size[0], 75), fill=SEA_COLOR)
        draw.text((40, 12), title, fill=BOUNDARY_COLOR, font=_font(46))
        image.paste(map_image, (panel_width, 0))
        draw = ImageDraw.Draw(image)
        if legend_height:
            self._draw_scale_legend(draw)
        list_image = Image.new("RGB", (list_width, list_height), SEA_COLOR)
        self._draw_region_list(ImageDraw.Draw(list_image), groups, code, 0, column_width)
        available_height = max(1, map_size[1] - legend_height)
        list_image.thumbnail((panel_width, available_height), Image.Resampling.LANCZOS)
        image.paste(list_image, (0, legend_height))
        footer_font = _font(24)
        footer_y = image.height - 65
        draw.text(
            (30, footer_y),
            "出典：気象庁 予報区等GISデータを加工して作成",
            fill=BOUNDARY_COLOR,
            font=footer_font,
        )
        draw.text((30, footer_y + 30), "https://www.data.jma.go.jp/developer/gis.html", fill=BOUNDARY_COLOR, font=footer_font)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def _regions(self, code: int, event: dict[str, Any], by_name: dict[str, str]) -> dict[str, tuple[tuple[int, int, int], str]]:
        result: dict[str, tuple[tuple[int, int, int], str]] = {}
        if code == 552:
            for area in event.get("areas", []):
                name = area.get("name")
                grade = area.get("grade")
                if name not in by_name:
                    LOG.warning("Skipping unmapped tsunami area: name=%s", name)
                    continue
                if grade not in TSUNAMI_GRADES:
                    LOG.warning("Skipping tsunami area with unknown grade: name=%s grade=%s", name, grade)
                    continue
                label, color, priority = TSUNAMI_GRADES[grade]
                boundary = by_name[name]
                previous = result.get(boundary)
                if previous is None or priority > TSUNAMI_GRADE_PRIORITY[previous[1]]:
                    result[boundary] = (color, label)
            return result

        if code == 556:
            for area in event.get("areas", []):
                name = area.get("name")
                scale = area.get("scaleTo")
                if name not in by_name:
                    LOG.warning("Skipping unmapped EEW warning area: name=%s", name)
                    continue
                label = f"予想{SCALE_LABELS_TEXT[scale]}" if scale in SCALE_LABELS_TEXT else "予想震度不明"
                result[by_name[name]] = (EEW_WARNING_COLOR, label)
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
                if not boundary:
                    LOG.warning(
                        "Skipping unmapped observation point: prefecture=%s name=%s",
                        point.get("pref"), point.get("addr"),
                    )
            if boundary:
                scales[boundary] = max(scale, scales.get(boundary, 0))
        for boundary, scale in scales.items():
            result[boundary] = (SCALE_RGB[scale], SCALE_LABELS_TEXT[scale])
        return result

    @staticmethod
    def _draw_region_labels(
        draw: ImageDraw.ImageDraw,
        selected: list[tuple[Any, str, tuple[int, int, int], str]],
        projection: _Projection,
    ) -> None:
        font = _font(27)
        occupied: list[tuple[float, float, float, float]] = []
        for shape, _, _, label in selected:
            point = _line_midpoint(shape, projection)
            if point is None:
                continue
            x, y = point
            text = label
            box = draw.textbbox((x, y - 10), text, font=font, anchor="mb", stroke_width=3)
            if box[0] < 0 or box[2] >= projection.size[0] or box[1] < 75 or box[3] >= projection.size[1]:
                continue
            if any(box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1] for b in occupied):
                continue
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(15, 20, 25), outline="white", width=2)
            draw.text((x, y - 10), text, font=font, anchor="mb", fill=(15, 20, 25), stroke_width=3, stroke_fill="white")
            occupied.append(box)

    @staticmethod
    def _draw_intensity_markers(
        draw: ImageDraw.ImageDraw,
        selected: list[tuple[Any, str, tuple[int, int, int], str]],
        projection: _Projection,
        code: int,
    ) -> None:
        labels = {
            (f"予想{label}" if code == 556 else label): scale
            for scale, label in SCALE_LABELS_TEXT.items()
        }
        occupied: list[tuple[float, float, float, float]] = []
        ordered = sorted(selected, key=lambda item: -labels.get(item[3], 0))
        for shape, _, _, label in ordered:
            scale = labels.get(label)
            if scale is None:
                continue
            point = _interior_point(shape)
            if point is None:
                continue
            x, y = projection(*point)
            box = (round(x) - 22, round(y) - 22, round(x) + 22, round(y) + 22)
            if box[0] < 0 or box[2] >= projection.size[0] or box[1] < 75 or box[3] >= projection.size[1]:
                continue
            if any(box[0] <= b[2] and box[2] >= b[0] and box[1] <= b[3] and box[3] >= b[1] for b in occupied):
                continue
            color = SCALE_RGB[scale]
            text_color = (15, 20, 25) if 299 * color[0] + 587 * color[1] + 114 * color[2] >= 150000 else (255, 255, 255)
            draw.rectangle(box, fill=color, outline=(15, 20, 25), width=3)
            draw.rectangle((box[0] + 3, box[1] + 3, box[2] - 3, box[3] - 3), outline=BOUNDARY_COLOR, width=2)
            text = SCALE_MARKERS[scale]
            font, (left, top, right, bottom) = _marker_font(draw, text)
            draw.text((round(x) - (left + right) / 2, round(y) - (top + bottom) / 2), text, fill=text_color, font=font)
            occupied.append(box)

    @staticmethod
    def _region_groups(
        selected: list[tuple[Any, str, tuple[int, int, int], str]], code: int,
    ) -> list[tuple[str, tuple[int, int, int], list[str]]]:
        grouped: dict[str, tuple[tuple[int, int, int], list[str]]] = {}
        for _, name, color, label in selected:
            if label not in grouped:
                grouped[label] = (color, [])
            grouped[label][1].append(name)
        rank = {label: scale for scale, label in SCALE_LABELS_TEXT.items()}
        if code == 556:
            rank = {f"予想{label}": scale for scale, label in SCALE_LABELS_TEXT.items()}
        if code == 552:
            rank = TSUNAMI_GRADE_PRIORITY
        return [
            (label, color, sorted(names))
            for label, (color, names) in sorted(grouped.items(), key=lambda item: (-rank.get(item[0], 0), item[0]))
        ]

    @staticmethod
    def _region_column_width(groups: list[tuple[str, tuple[int, int, int], list[str]]]) -> int:
        font = _font(27)
        return max(200, max((math.ceil(font.getlength(name)) + 20 for _, _, names in groups for name in names), default=0))

    @staticmethod
    def _draw_region_list(
        draw: ImageDraw.ImageDraw,
        groups: list[tuple[str, tuple[int, int, int], list[str]]],
        code: int,
        top: int = 0,
        column_width: int = 200,
    ) -> None:
        title = "各地域内で観測された最大震度" if code == 551 else ("地域別の予想震度" if code == 556 else "津波予報区")
        draw.text((30, top + 20), title, fill=BOUNDARY_COLOR, font=_font(30))
        font = _font(27)
        y = top + 90
        for label, color, names in groups:
            draw.rectangle((30, y + 5, 54, y + 29), fill=color, outline=BOUNDARY_COLOR)
            draw.text((65, y), label, fill=BOUNDARY_COLOR, font=font)
            y += 48
            for index, name in enumerate(names):
                x = 65 + (index % REGION_COLUMNS) * column_width
                draw.text((x, y + (index // REGION_COLUMNS) * 48), name, fill=BOUNDARY_COLOR, font=font)
            y += 48 * math.ceil(len(names) / REGION_COLUMNS)

    @staticmethod
    def _draw_scale_legend(draw: ImageDraw.ImageDraw) -> None:
        font = _font(28)
        draw.rectangle((20, 20, PANEL_WIDTH - 20, LEGEND_HEIGHT - 20), fill=SEA_COLOR, outline=BOUNDARY_COLOR, width=2)
        for index, (scale, color) in enumerate(SCALE_RGB.items()):
            column, row = index % 3, index // 3
            x, y = 36 + column * 195, 38 + row * 56
            draw.rectangle((x, y + 4, x + 34, y + 36), fill=color, outline=(80, 80, 80))
            draw.text((x + 44, y), SCALE_LABELS_TEXT[scale], fill=BOUNDARY_COLOR, font=font)

    def _draw_land(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[float, float, float, float],
        projection: _Projection,
    ) -> None:
        for shape in self._iter_land_shapes(self.seismic_path, bounds):
            _draw_shape(draw, shape, projection, LAND_COLOR, outline=BOUNDARY_COLOR)

    @staticmethod
    @lru_cache(maxsize=2)
    def _read_boundary_index(path: Path) -> tuple[dict[str, str], dict[str, tuple[int, str]]]:
        with shapefile.Reader(str(path), encoding="utf-8") as reader:
            field_names = [field[0] for field in reader.fields[1:]]
            name_field = next((name for name in ("name", "NAME") if name in field_names), None)
            code_field = next((name for name in ("code", "CODE") if name in field_names), None)
            if name_field is None or code_field is None:
                raise ValueError(f"GIS fields missing name/code: {field_names}")
            by_name: dict[str, str] = {}
            by_code: dict[str, tuple[int, str]] = {}
            for record in reader.iterRecords(fields=[name_field, code_field]):
                name, code = str(record[name_field]), str(record[code_field])
                by_name[name] = code
                by_code[code] = (record.oid, name)
            return by_name, by_code

    @staticmethod
    def _read_selected_shapes(
        path: Path, selected: list[tuple[int, str, tuple[int, int, int], str]],
    ) -> list[tuple[Any, str, tuple[int, int, int], str]]:
        with shapefile.Reader(str(path), encoding="utf-8") as reader:
            return [(reader.shape(index), name, color, label) for index, name, color, label in selected]

    @staticmethod
    def _iter_land_shapes(path: Path, bounds: tuple[float, float, float, float]) -> Iterator[Any]:
        with shapefile.Reader(str(path), encoding="utf-8") as reader:
            yield from reader.iterShapes(bbox=bounds)


def _valid_coords(lon: Any, lat: Any) -> tuple[float | None, float | None]:
    try:
        longitude, latitude = float(lon), float(lat)
    except (TypeError, ValueError):
        return None, None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90) or longitude == -200:
        return None, None
    return longitude, latitude


def _mercator_y(latitude: float) -> float:
    latitude = max(-MERCATOR_MAX_LATITUDE, min(MERCATOR_MAX_LATITUDE, latitude))
    return math.degrees(math.asinh(math.tan(math.radians(latitude))))


def _expanded_bounds(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = bounds
    dx, dy = max(max_x - min_x, 0.4), max(max_y - min_y, 0.4)
    margin_x = min(max(dx * 0.18, 0.25), 1.0)
    margin_y = min(max(dy * 0.18, 0.25), 1.0)
    return (
        min_x - margin_x, max(-MERCATOR_MAX_LATITUDE, min_y - margin_y),
        max_x + margin_x, min(MERCATOR_MAX_LATITUDE, max_y + margin_y),
    )


def _map_layout(
    bounds: tuple[float, float, float, float], size: tuple[int, int],
) -> tuple[tuple[int, int], int]:
    min_x, min_y, max_x, max_y = bounds
    width, height = size
    projected_height = _mercator_y(max_y) - _mercator_y(min_y)
    if width <= 1000 or max(max_x - min_x, projected_height) <= 5:
        return size, 40
    if projected_height <= 0:
        raise ValueError("Invalid Mercator map bounds")
    desired_width = math.ceil(40 + (height - 150) * (max_x - min_x) / projected_height)
    return (min(width, max(1000, desired_width)), height), 100


class _Projection:
    def __init__(self, bounds: tuple[float, float, float, float], size: tuple[int, int], top_padding: int = 40) -> None:
        min_x, min_y, max_x, max_y = bounds
        width, height = size
        self.size = size
        self.min_x = min_x
        self.max_y = _mercator_y(max_y)
        projected_height = self.max_y - _mercator_y(min_y)
        if max_x <= min_x or projected_height <= 0:
            raise ValueError("Invalid Mercator map bounds")
        self.scale = min((width - 40) / (max_x - min_x), (height - top_padding - 50) / projected_height)
        self.offset_x = (width - (max_x - min_x) * self.scale) / 2
        self.offset_y = top_padding + (height - top_padding - 50 - projected_height * self.scale) / 2

    def __call__(self, longitude: float, latitude: float) -> tuple[float, float]:
        return (
            self.offset_x + (longitude - self.min_x) * self.scale,
            self.offset_y + (self.max_y - _mercator_y(latitude)) * self.scale,
        )

    def visible_bounds(self, size: tuple[int, int]) -> tuple[float, float, float, float]:
        width, height = size
        west = max(-180.0, self.min_x - self.offset_x / self.scale)
        east = min(180.0, self.min_x + (width - self.offset_x) / self.scale)
        top = self.max_y + self.offset_y / self.scale
        bottom = self.max_y - (height - self.offset_y) / self.scale

        def latitude(projected: float) -> float:
            limit = _mercator_y(MERCATOR_MAX_LATITUDE)
            if projected >= limit:
                return 90.0
            if projected <= -limit:
                return -90.0
            return math.degrees(math.atan(math.sinh(math.radians(projected))))

        return west, latitude(bottom), east, latitude(top)


def _interior_point(shape: Any) -> tuple[float, float] | None:
    parts = list(shape.parts) + [len(shape.points)]
    best: tuple[float, float, float] | None = None
    for start, end in zip(parts, parts[1:]):
        ring = shape.points[start:end]
        if len(ring) < 3:
            continue
        min_y, max_y = min(point[1] for point in ring), max(point[1] for point in ring)
        for fraction in (0.25, 0.5, 0.75):
            y = min_y + (max_y - min_y) * fraction
            intersections = []
            for first, second in zip(ring, ring[1:] + ring[:1]):
                x1, y1 = first
                x2, y2 = second
                if (y1 > y) != (y2 > y):
                    intersections.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
            intersections.sort()
            for x1, x2 in zip(intersections[::2], intersections[1::2]):
                if best is None or x2 - x1 > best[0]:
                    best = (x2 - x1, (x1 + x2) / 2, y)
    return (best[1], best[2]) if best else None


def _draw_hypocenter(draw: ImageDraw.ImageDraw, x: float, y: float) -> None:
    diagonals = ((x - 16, y - 16, x + 16, y + 16), (x - 16, y + 16, x + 16, y - 16))
    for line in diagonals:
        draw.line(line, fill="white", width=14)
    for line in diagonals:
        draw.line(line, fill=(200, 0, 0), width=8)


def _marker_font(
    draw: ImageDraw.ImageDraw, text: str,
) -> tuple[ImageFont.FreeTypeFont, tuple[int, int, int, int]]:
    for size in range(27, 9, -1):
        font = _font(size)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= 34 and box[3] - box[1] <= 34:
            return font, box
    raise ValueError(f"Intensity marker text does not fit: {text}")


def _draw_shape(
    draw: ImageDraw.ImageDraw,
    shape: Any,
    projection: _Projection,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
) -> None:
    indices = list(shape.parts) + [len(shape.points)]
    for start, end in zip(indices, indices[1:]):
        ring = [projection(x, y) for x, y in shape.points[start:end]]
        if len(ring) >= 3:
            draw.polygon(ring, fill=fill, outline=outline)


def _draw_boundary(
    draw: ImageDraw.ImageDraw, shape: Any, projection: _Projection,
    color: tuple[int, int, int] = BOUNDARY_COLOR, width: int = 1,
) -> None:
    indices = list(shape.parts) + [len(shape.points)]
    for start, end in zip(indices, indices[1:]):
        line = [projection(x, y) for x, y in shape.points[start:end]]
        if len(line) >= 2:
            draw.line(line, fill=color, width=width)


def _line_midpoint(shape: Any, projection: _Projection) -> tuple[float, float] | None:
    indices = list(shape.parts) + [len(shape.points)]
    best: tuple[float, tuple[float, float], tuple[float, float]] | None = None
    for start, end in zip(indices, indices[1:]):
        line = shape.points[start:end]
        for first, second in zip(line, line[1:]):
            a, b = projection(*first), projection(*second)
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if best is None or length > best[0]:
                best = (length, a, b)
    if best is None or best[0] == 0:
        return None
    return ((best[1][0] + best[2][0]) / 2, (best[1][1] + best[2][1]) / 2)


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        Path("/usr/share/fonts/google-noto-cjk/NotoSansCJKjp-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("C:/Windows/Fonts/meiryo.ttc"),
        Path("C:/Windows/Fonts/YuGothR.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    raise OSError("Japanese font not found; install google-noto-sans-cjk-jp-fonts")
