from __future__ import annotations

import io
import logging
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import shapefile
from PIL import Image, ImageDraw

from awoke import map_render
from awoke.map_render import MAP_SIZE, SCALE_RGB, MapRenderer


def _renderer(tmp_path: Path) -> MapRenderer:
    mapping = tmp_path / "mapping.csv"
    mapping.write_text(
        "code,status,prefecture,source_name,boundary_code\n"
        "551,mapped,茨城県,観測点,221\n",
        encoding="utf-8",
    )
    return MapRenderer(mapping, tmp_path / "seismic.shp", tmp_path / "tsunami.shp", tmp_path / "land.shp")


def _write_shapes(path: Path) -> None:
    with shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        writer.field("name", "C", size=40)
        writer.field("code", "C", size=10)
        writer.poly([[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]])
        writer.record("区域A", "1")
        writer.poly([[(10, 10), (11, 10), (11, 11), (10, 11), (10, 10)]])
        writer.record("区域B", "2")


def test_boundary_index_loads_only_selected_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "boundaries.shp"
    _write_shapes(path)
    loaded: list[int] = []
    original_shape = shapefile.Reader.shape

    def tracked_shape(reader: shapefile.Reader, index: int, *args: Any, **kwargs: Any) -> Any:
        loaded.append(index)
        return original_shape(reader, index, *args, **kwargs)

    monkeypatch.setattr(shapefile.Reader, "shape", tracked_shape)
    by_name, by_code = MapRenderer._read_boundary_index(path)
    assert by_name == {"区域A": "1", "区域B": "2"}
    assert by_code == {"1": (0, "区域A"), "2": (1, "区域B")}
    assert loaded == []
    selected = MapRenderer._read_selected_shapes(path, [(1, "区域B", SCALE_RGB[30], "震度3")])
    assert loaded == [1]
    assert list(selected[0][0].bbox) == [10.0, 10.0, 11.0, 11.0]
    MapRenderer._read_boundary_index.cache_clear()


def test_land_shapes_only_include_intersecting_bounds(tmp_path: Path) -> None:
    path = tmp_path / "land.shp"
    _write_shapes(path)
    shapes = list(MapRenderer._iter_land_shapes(path, (-1, -1, 2, 2)))
    assert len(shapes) == 1
    assert list(shapes[0].bbox) == [0.0, 0.0, 1.0, 1.0]


@pytest.mark.parametrize("size,extra,probe", [
    ((400, 200), (-0.6, 0.3, -0.2, 0.7), (-0.4, 0.5)),
    ((200, 400), (0.3, -0.7, 0.7, -0.3), (0.5, -0.5)),
])
@pytest.mark.parametrize("code", [551, 552, 556])
def test_unselected_land_in_visible_margins_is_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int], extra: tuple[float, float, float, float],
    probe: tuple[float, float], code: int,
) -> None:
    renderer = _renderer(tmp_path)
    for path in (renderer.seismic_path, renderer.tsunami_path):
        with shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
            writer.field("name", "C", size=40)
            writer.field("code", "C", size=10)
            for name, region_code, box in [("対象", "1", (0.25, 0.25, 0.75, 0.75)), ("周辺", "2", extra)]:
                x1, y1, x2, y2 = box
                writer.poly([[(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]])
                writer.record(name, region_code)
    loaded: dict[Path, list[list[float]]] = {}
    original_iter = renderer._iter_land_shapes

    def track_shapes(path: Path, bounds: tuple[float, float, float, float]) -> Any:
        shapes = list(original_iter(path, bounds))
        loaded[path] = [list(shape.bbox) for shape in shapes]
        return iter(shapes)

    monkeypatch.setattr(renderer, "_iter_land_shapes", track_shapes)
    monkeypatch.setattr(map_render, "MAP_SIZE", size)
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(renderer, "_region_column_width", lambda groups: 200)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *args, **kwargs: None)
    monkeypatch.setattr(renderer, "_draw_region_labels", lambda *args: None)
    monkeypatch.setattr(renderer, "_draw_scale_legend", lambda *args: None)
    monkeypatch.setattr(renderer, "_draw_region_list", lambda *args: None)
    event = (
        {"points": [{"isArea": True, "addr": "対象", "scale": 30}]} if code == 551
        else {"areas": [{"name": "対象", "grade": "Warning", "scaleTo": 30}]}
    )
    png = renderer.render(code, event)
    assert png is not None
    assert list(extra) in loaded[renderer.seismic_path]
    if code == 552:
        assert list(extra) in loaded[renderer.tsunami_path]
    projection = map_render._Projection((0, 0, 1, 1), size)
    x, y = projection(*probe)
    assert 0 < x < size[0] and 75 < y < size[1]
    with Image.open(io.BytesIO(png)) as image:
        assert image.getpixel((665 + round(x), round(y))) == map_render.LAND_COLOR
        selected_x, selected_y = projection(0.5, 0.5)
        expected = (255, 153, 0) if code == 552 else (map_render.EEW_WARNING_COLOR if code == 556 else SCALE_RGB[30])
        assert image.getpixel((665 + round(selected_x), round(selected_y))) == expected


def test_observation_regions_use_maximum_intensity_and_correct_six_lower(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)
    event = {"points": [
        {"isArea": True, "addr": "茨城県南部", "scale": 40},
        {"isArea": False, "pref": "茨城県", "addr": "観測点", "scale": 55},
    ]}
    regions = renderer._regions(551, event, {"茨城県南部": "221"})
    assert SCALE_RGB[55] == (230, 0, 0)
    assert regions == {"221": ((230, 0, 0), "震度6弱")}


@pytest.mark.parametrize("scale,color", [
    (10, (70, 100, 110)),
    (20, (30, 110, 230)),
    (30, (0, 200, 200)),
    (40, (250, 250, 100)),
    (45, (255, 180, 0)),
    (50, (255, 120, 0)),
    (55, (230, 0, 0)),
    (60, (160, 0, 0)),
    (70, (150, 0, 150)),
])
def test_observation_palette_matches_rendered_region_and_legend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scale: int, color: tuple[int, int, int],
) -> None:
    renderer = _renderer(tmp_path)
    regions = renderer._regions(551, {"points": [{"isArea": True, "addr": "区域", "scale": scale}]}, {"区域": "1"})
    assert regions["1"] == (color, map_render.SCALE_LABELS_TEXT[scale])
    shape = SimpleNamespace(
        parts=[0], points=[(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75), (0.25, 0.25)],
    )
    map_image = Image.new("RGB", (200, 200), map_render.SEA_COLOR)
    map_render._draw_shape(
        ImageDraw.Draw(map_image), shape, map_render._Projection((0, 0, 1, 1), map_image.size),
        regions["1"][0], map_render.BOUNDARY_COLOR,
    )
    assert map_image.getpixel((100, 95)) == color

    image = Image.new("RGB", (map_render.PANEL_WIDTH, map_render.LEGEND_HEIGHT), map_render.SEA_COLOR)
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *args, **kwargs: None)
    MapRenderer._draw_scale_legend(ImageDraw.Draw(image))
    index = list(SCALE_RGB).index(scale)
    assert image.getpixel((40 + (index % 3) * 195, 46 + (index // 3) * 56)) == color


def test_forecast_and_tsunami_labels_distinguish_information_types(tmp_path: Path) -> None:
    renderer = _renderer(tmp_path)
    names = {"茨城県南部": "221"}
    forecast = renderer._regions(556, {"areas": [{"name": "茨城県南部", "scaleTo": 55}]}, names)
    tsunami = renderer._regions(552, {"areas": [{"name": "茨城県南部", "grade": "Warning"}]}, names)
    assert forecast["221"][1] == "予想震度6弱"
    assert forecast["221"][0] == map_render.EEW_WARNING_COLOR
    assert tsunami["221"][1] == "津波警報"


@pytest.mark.parametrize("scale", [10, 40, 70, -1, None])
def test_eew_regions_use_warning_color_regardless_of_forecast(
    tmp_path: Path, scale: int | None,
) -> None:
    area = {"name": "茨城県南部", "scaleTo": scale}
    regions = _renderer(tmp_path)._regions(556, {"areas": [area]}, {"茨城県南部": "221"})
    label = f"予想{map_render.SCALE_LABELS_TEXT[scale]}" if scale in map_render.SCALE_LABELS_TEXT else "予想震度不明"
    assert regions == {"221": (map_render.EEW_WARNING_COLOR, label)}


def test_eew_missing_scale_and_unmapped_area(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _renderer(tmp_path)
    event = {"areas": [{"name": "区域"}, {"name": "未対応", "scaleTo": -1}]}
    with caplog.at_level(logging.WARNING, logger="awoke.map_render"):
        regions = renderer._regions(556, event, {"区域": "1"})
    assert regions == {"1": (map_render.EEW_WARNING_COLOR, "予想震度不明")}
    assert "未対応" in caplog.text
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: ({"区域": "1"}, {"1": (0, "区域")}))
    assert renderer.render(556, {"areas": [{"name": "未対応"}]}) is None
    assert "No matching regional boundary" in caplog.text


def test_eew_groups_keep_predicted_intensity_order_and_unknown_last() -> None:
    selected = [
        (None, "不明", map_render.EEW_WARNING_COLOR, "予想震度不明"),
        (None, "低", map_render.EEW_WARNING_COLOR, "予想震度2"),
        (None, "高", map_render.EEW_WARNING_COLOR, "予想震度5弱"),
    ]
    assert MapRenderer._region_groups(selected, 556) == [
        ("予想震度5弱", map_render.EEW_WARNING_COLOR, ["高"]),
        ("予想震度2", map_render.EEW_WARNING_COLOR, ["低"]),
        ("予想震度不明", map_render.EEW_WARNING_COLOR, ["不明"]),
    ]


def test_unmapped_observation_is_skipped_when_other_points_are_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    renderer = _renderer(tmp_path)
    shape = SimpleNamespace(
        parts=[0], bbox=(0, 0, 1, 1), points=[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
    )
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: (
        {"茨城県南部": "221"}, {"221": (0, "茨城県南部")},
    ))
    monkeypatch.setattr(renderer, "_read_selected_shapes", lambda path, selected: [
        (shape, name, color, label) for _, name, color, label in selected
    ])
    monkeypatch.setattr(renderer, "_iter_land_shapes", lambda path, bounds: iter([]))
    monkeypatch.setattr(map_render, "MAP_SIZE", (200, 200))
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(renderer, "_region_column_width", lambda groups: 200)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *args, **kwargs: None)
    monkeypatch.setattr(renderer, "_draw_region_labels", lambda *args: None)
    monkeypatch.setattr(renderer, "_draw_scale_legend", lambda *args: None)
    monkeypatch.setattr(renderer, "_draw_region_list", lambda *args: None)
    event = {"points": [
        {"isArea": False, "pref": "鹿児島県", "addr": "鹿児島十島村中之島徳之尾", "scale": 45},
        {"isArea": False, "pref": "茨城県", "addr": "観測点", "scale": 30},
    ]}
    with caplog.at_level(logging.WARNING, logger="awoke.map_render"):
        image_bytes = renderer.render(551, event)
    assert image_bytes is not None
    with Image.open(io.BytesIO(image_bytes)) as image:
        assert image.getpixel((665 + 100, 95)) == SCALE_RGB[30]
    assert "鹿児島十島村中之島徳之尾" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_all_unmapped_observations_skip_map_without_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    renderer = _renderer(tmp_path)
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: ({}, {}))
    event = {"points": [
        {"isArea": False, "pref": "鹿児島県", "addr": "鹿児島十島村中之島徳之尾", "scale": 45},
        {"isArea": False, "pref": "鹿児島県", "addr": "屋久島町平内", "scale": 20},
    ]}
    with caplog.at_level(logging.WARNING, logger="awoke.map_render"):
        assert renderer.render(551, event) is None
    assert "鹿児島十島村中之島徳之尾" in caplog.text
    assert "屋久島町平内" in caplog.text
    assert "sending without a map" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_mapped_observation_with_missing_gis_boundary_remains_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    renderer = _renderer(tmp_path)
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: ({}, {}))
    event = {"points": [{"isArea": False, "pref": "茨城県", "addr": "観測点", "scale": 30}]}
    with caplog.at_level(logging.ERROR, logger="awoke.map_render"):
        assert renderer.render(551, event) is None
    assert "Map rendering failed: No matching regional boundary" in caplog.text


def test_region_list_displays_every_name_and_intensity(monkeypatch: pytest.MonkeyPatch) -> None:
    texts: list[str] = []

    class Draw:
        im = SimpleNamespace(size=(MAP_SIZE[0], MAP_SIZE[1] + 500))

        def rectangle(self, *args: Any, **kwargs: Any) -> None:
            pass

        def text(self, xy: Any, text: str, **kwargs: Any) -> None:
            texts.append(text)

    monkeypatch.setattr(map_render, "_font", lambda size: None)
    selected = [
        (None, "低震度", SCALE_RGB[20], "震度2"),
        (None, "高震度A", SCALE_RGB[55], "震度6弱"),
        (None, "高震度B", SCALE_RGB[55], "震度6弱"),
        (None, "中震度", SCALE_RGB[45], "震度5弱"),
    ]
    groups = MapRenderer._region_groups(selected, 551)
    MapRenderer._draw_region_list(Draw(), groups, 551)
    assert "各地域内で観測された最大震度" in texts
    assert texts[1:] == ["震度6弱", "高震度A", "高震度B", "震度5弱", "中震度", "震度2", "低震度"]


def test_legend_is_in_left_panel_above_region_list(monkeypatch: pytest.MonkeyPatch) -> None:
    texts: list[tuple[tuple[int, int], str]] = []
    rectangles: list[tuple[int, int, int, int]] = []

    class Draw:
        def rectangle(self, box: tuple[int, int, int, int], **kwargs: Any) -> None:
            rectangles.append(box)

        def text(self, xy: tuple[int, int], text: str, **kwargs: Any) -> None:
            texts.append((xy, text))

    monkeypatch.setattr(map_render, "_font", lambda size: None)
    draw = Draw()
    MapRenderer._draw_scale_legend(draw)
    MapRenderer._draw_region_list(draw, [("震度3", SCALE_RGB[30], ["区域"])], 551, map_render.LEGEND_HEIGHT)
    assert all(0 <= box[0] < box[2] < map_render.PANEL_WIDTH for box in rectangles)
    assert all(x < map_render.PANEL_WIDTH and y < map_render.LEGEND_HEIGHT for (x, y), _ in texts[:9])
    assert texts[9][0][1] > max(y for (_, y), _ in texts[:9])
    assert texts[9][1] == "各地域内で観測された最大震度"
    assert {text for _, text in texts[:9]} == set(map_render.SCALE_LABELS_TEXT.values())


def test_three_columns_keep_groups_order_and_fit_all_names(monkeypatch: pytest.MonkeyPatch) -> None:
    texts: list[tuple[tuple[int, int], str]] = []

    class Draw:
        def rectangle(self, *args: Any, **kwargs: Any) -> None:
            pass

        def text(self, xy: tuple[int, int], text: str, **kwargs: Any) -> None:
            texts.append((xy, text))

    monkeypatch.setattr(map_render, "_font", lambda size: None)
    groups = [
        ("震度5弱", SCALE_RGB[45], [f"地域{i}" for i in range(7)]),
        ("震度2", SCALE_RGB[20], ["次の地域"]),
    ]
    MapRenderer._draw_region_list(Draw(), groups, 551, 240, 260)
    by_name = {text: pos for pos, text in texts}
    assert [by_name[f"地域{i}"] for i in range(4)] == [
        (65, 378), (325, 378), (585, 378), (65, 426),
    ]
    assert by_name["震度2"][1] > by_name["地域6"][1]
    assert len({text for _, text in texts if text.startswith("地域")}) == 7


def test_region_column_width_adapts_to_long_names(monkeypatch: pytest.MonkeyPatch) -> None:
    class Font:
        def getlength(self, text: str) -> float:
            return len(text) * 27.0

    monkeypatch.setattr(map_render, "_font", lambda size: Font())
    groups = [("震度3", SCALE_RGB[30], ["地域", "とても長い地域名" * 3])]
    width = MapRenderer._region_column_width(groups)
    assert width >= len("とても長い地域名" * 3) * 27 + 20


@pytest.mark.parametrize("code", [551, 552, 556])
def test_large_region_lists_do_not_change_map_image_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int,
) -> None:
    renderer = _renderer(tmp_path)
    size = (400, 300)
    shape = SimpleNamespace(
        parts=[0], bbox=(0.25, 0.25, 0.75, 0.75),
        points=[(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75), (0.25, 0.25)],
    )
    names = [f"region-{i:03d}" for i in range(150)]
    index = {name: str(i) for i, name in enumerate(names)}
    by_code = {str(i): (i, name) for i, name in enumerate(names)}
    monkeypatch.setattr(map_render, "MAP_SIZE", size)
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(renderer, "_region_column_width", lambda groups: 800 if len(groups[0][2]) > 1 else 200)
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: (index, by_code))
    monkeypatch.setattr(renderer, "_read_selected_shapes", lambda path, selected: [
        (shape, name, color, label) for _, name, color, label in selected
    ])
    monkeypatch.setattr(renderer, "_iter_land_shapes", lambda path, bounds: iter([]))
    monkeypatch.setattr(renderer, "_draw_region_labels", lambda *args: None)
    drawn: list[str] = []
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda self, xy, text, **kwargs: drawn.append(text))

    def event(selected: list[str]) -> dict[str, Any]:
        if code == 551:
            return {"points": [{"isArea": True, "addr": name, "scale": 30} for name in selected]}
        return {"areas": [{"name": name, "grade": "Warning", "scaleTo": 30} for name in selected]}

    for selected in (names[:1], names):
        drawn.clear()
        png = renderer.render(code, event(selected))
        assert png is not None
        assert all(name in drawn for name in selected)
        with Image.open(io.BytesIO(png)) as image:
            assert image.size == (665 + size[0], size[1] + 80)
            expected = SCALE_RGB[30] if code == 551 else (
                (255, 153, 0) if code == 552 else map_render.EEW_WARNING_COLOR
            )
            assert image.getpixel((665 + 200, 150)) == expected


def test_missing_japanese_font_reports_error_instead_of_default(monkeypatch: pytest.MonkeyPatch) -> None:
    map_render._font.cache_clear()
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(OSError, match="Japanese font not found"):
        map_render._font(24)
    map_render._font.cache_clear()


def test_region_label_anchor_is_inside_concave_polygon() -> None:
    shape = SimpleNamespace(
        parts=[0],
        points=[(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4), (0, 0)],
    )
    x, y = map_render._interior_point(shape)
    assert 0 < x < 4 and 0 < y < 4
    assert x < 1 or y < 1


def test_hypocenter_marker_is_a_cross_not_a_circle() -> None:
    image = Image.new("RGB", (81, 81), (0, 0, 0))
    map_render._draw_hypocenter(ImageDraw.Draw(image), 40, 40)
    assert image.getpixel((40, 40)) == (200, 0, 0)
    assert image.getpixel((28, 28)) == (200, 0, 0)
    assert image.getpixel((28, 52)) == (200, 0, 0)
    assert image.getpixel((40, 28)) == (0, 0, 0)


@pytest.mark.parametrize("scale", [30, 55, -1, None])
def test_render_uses_dark_sea_gray_land_and_light_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scale: int | None,
) -> None:
    renderer = _renderer(tmp_path)
    region = SimpleNamespace(
        parts=[0],
        bbox=(0.25, 0.25, 0.75, 0.75),
        points=[(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75), (0.25, 0.25)],
    )
    land = SimpleNamespace(
        parts=[0],
        bbox=(0.05, 0.05, 0.20, 0.95),
        points=[(0.05, 0.05), (0.20, 0.05), (0.20, 0.95), (0.05, 0.95), (0.05, 0.05)],
    )
    unselected = SimpleNamespace(
        parts=[0],
        points=[(0.80, 0.10), (0.90, 0.10), (0.90, 0.90), (0.80, 0.90), (0.80, 0.10)],
    )
    monkeypatch.setattr(map_render, "MAP_SIZE", (200, 200))
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(renderer, "_region_column_width", lambda groups: 200)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *args, **kwargs: None)
    monkeypatch.setattr(renderer, "_draw_region_labels", lambda *args: None)
    def unexpected_legend(*args: Any) -> None:
        pytest.fail("EEW map must not show the intensity color legend")

    monkeypatch.setattr(renderer, "_draw_scale_legend", unexpected_legend)
    monkeypatch.setattr(renderer, "_draw_region_list", lambda *args: None)
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: ({"区域": "1"}, {"1": (0, "区域")}))
    monkeypatch.setattr(renderer, "_read_selected_shapes", lambda path, selected: [
        (region, name, color, label) for _, name, color, label in selected
    ])
    monkeypatch.setattr(renderer, "_iter_land_shapes", lambda path, bounds: iter([land, unselected, region]))
    image = Image.open(io.BytesIO(renderer._render(556, {"areas": [{"name": "区域", "scaleTo": scale}]})))
    offset = 665
    assert image.getpixel((offset + 190, 95)) == (23, 30, 37)
    assert image.getpixel((offset + 57, 95)) == (105, 111, 117)
    assert image.getpixel((offset + 50, 95)) == (225, 230, 235)
    assert image.getpixel((offset + 72, 95)) == (225, 230, 235)
    assert image.getpixel((offset + 100, 95)) == map_render.EEW_WARNING_COLOR
    boundary_x = int(map_render._Projection((0, 0, 1, 1), (200, 200))(0.8, 0.5)[0])
    assert image.getpixel((offset + boundary_x, 95)) == (225, 230, 235)
    assert image.getpixel((offset + 139, 95)) == map_render.LAND_COLOR
    assert image.getpixel((0, 0)) == (23, 30, 37)


@pytest.mark.parametrize("code", [551, 552, 556])
def test_background_uses_jma_seismic_not_natural_earth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int,
) -> None:
    renderer = _renderer(tmp_path)
    region = SimpleNamespace(parts=[0], bbox=(0, 0, 1, 1), points=[
        (0, 0), (1, 0), (1, 1), (0, 1), (0, 0),
    ])
    loaded: list[Path] = []
    monkeypatch.setattr(map_render, "MAP_SIZE", (200, 200))
    monkeypatch.setattr(map_render, "_font", lambda size: None)
    monkeypatch.setattr(renderer, "_region_column_width", lambda groups: 200)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", lambda *args, **kwargs: None)
    monkeypatch.setattr(renderer, "_draw_region_labels", lambda *args: None)
    monkeypatch.setattr(renderer, "_draw_scale_legend", lambda *args: None)
    monkeypatch.setattr(renderer, "_draw_region_list", lambda *args: None)
    monkeypatch.setattr(renderer, "_read_boundary_index", lambda path: ({"区域": "1"}, {"1": (0, "区域")}))
    monkeypatch.setattr(renderer, "_read_selected_shapes", lambda path, selected: [
        (region, name, color, label) for _, name, color, label in selected
    ])

    def shapes(path: Path, bounds: tuple[float, float, float, float]) -> Any:
        loaded.append(path)
        return iter([region])

    monkeypatch.setattr(renderer, "_iter_land_shapes", shapes)
    if code == 551:
        event = {"points": [{"isArea": True, "addr": "区域", "scale": 30}]}
    elif code == 552:
        event = {"areas": [{"name": "区域", "grade": "Warning"}]}
    else:
        event = {"areas": [{"name": "区域", "scaleTo": 30}]}
    assert renderer.render(code, event) is not None
    assert loaded == ([renderer.seismic_path, renderer.tsunami_path] if code == 552 else [renderer.seismic_path])


def test_unselected_region_boundary_does_not_fill_land() -> None:
    image = Image.new("RGB", (200, 200), map_render.LAND_COLOR)
    shape = SimpleNamespace(parts=[0], points=[(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)])
    map_render._draw_boundary(ImageDraw.Draw(image), shape, map_render._Projection((0, 0, 1, 1), image.size))
    assert image.getpixel((72, 95)) == map_render.BOUNDARY_COLOR
    assert image.getpixel((100, 95)) == map_render.LAND_COLOR


def test_mercator_projection_has_expected_coordinates_and_preserves_angles() -> None:
    assert map_render._mercator_y(0) == pytest.approx(0, abs=1e-12)
    assert map_render._mercator_y(45) == pytest.approx(50.4989867105)
    assert map_render._mercator_y(-45) == pytest.approx(-50.4989867105)
    projection = map_render._Projection((130, 30, 150, 50), (1000, 1000))
    x, y = projection(140, 40)
    east, _ = projection(140.01, 40)
    _, north = projection(140, 40 + 0.01 * math.cos(math.radians(40)))
    assert east - x == pytest.approx(y - north, rel=0.001)
    assert projection(140, 50)[1] < y < projection(140, 30)[1]


def test_mercator_poles_are_clamped_and_invalid_bounds_rejected() -> None:
    assert map_render._mercator_y(90) == pytest.approx(map_render._mercator_y(map_render.MERCATOR_MAX_LATITUDE))
    assert map_render._mercator_y(-90) == pytest.approx(map_render._mercator_y(-map_render.MERCATOR_MAX_LATITUDE))
    with pytest.raises(ValueError, match="Invalid Mercator map bounds"):
        map_render._Projection((130, 90, 150, 90), (1000, 1000))


@pytest.mark.parametrize("size", [(2200, 1500), (600, 1600)])
def test_visible_bounds_cover_the_entire_map_canvas(size: tuple[int, int]) -> None:
    projection = map_render._Projection((135, 32, 139, 38), size)
    west, south, east, north = projection.visible_bounds(size)
    assert projection(west, north) == pytest.approx((0, 0), abs=1e-8)
    assert projection(east, south) == pytest.approx(size, abs=1e-8)


def test_visible_bounds_clamp_world_edges_without_overflow() -> None:
    size = (200, 1_000_000)
    projection = map_render._Projection((-179, -80, 179, 80), size)
    assert projection.visible_bounds(size) == (-180, -90, 180, 90)


def test_mercator_marker_and_region_share_coordinates() -> None:
    image = Image.new("RGB", (800, 800), map_render.SEA_COLOR)
    draw = ImageDraw.Draw(image)
    projection = map_render._Projection((135, 30, 145, 50), image.size)
    shape = SimpleNamespace(parts=[0], points=[
        (139, 39), (141, 39), (141, 41), (139, 41), (139, 39),
    ])
    map_render._draw_shape(draw, shape, projection, SCALE_RGB[30], map_render.BOUNDARY_COLOR)
    x, y = projection(140, 40)
    assert image.getpixel((round(x), round(y))) == SCALE_RGB[30]
    map_render._draw_hypocenter(draw, x, y)
    assert image.getpixel((round(x), round(y))) == (200, 0, 0)
