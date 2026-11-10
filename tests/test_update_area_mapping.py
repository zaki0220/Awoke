from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import shapefile

from awoke.map_render import MapRenderer, SCALE_RGB
from tools.update_area_mapping import FIELDS, update_mapping


def _fixtures(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "stations.json"
    mapping = tmp_path / "mapping.csv"
    boundaries = tmp_path / "boundaries.shp"
    with shapefile.Writer(str(boundaries), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        writer.field("code", "C", size=3)
        writer.field("name", "C", size=30)
        writer.poly([[(0, 0), (1, 0), (1, 1), (0, 0)]])
        writer.record("774", "鹿児島県十島村")
    source.write_text(json.dumps([{
        "name": "鹿児島十島村中之島徳之尾",
        "pref": {"name": "鹿児島県"},
        "city": {"name": "十島村"},
        "area": {"code": "774", "name": "鹿児島県十島村"},
    }], ensure_ascii=False), encoding="utf-8")
    with mapping.open("w", encoding="utf-8-sig", newline="") as stream:
        csv.DictWriter(stream, fieldnames=FIELDS).writeheader()
    return source, mapping, boundaries


def test_adds_and_preserves_mapping_idempotently(tmp_path: Path) -> None:
    source, mapping, boundaries = _fixtures(tmp_path)
    assert update_mapping(source, mapping, boundaries) == 1
    with mapping.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert (row["prefecture"], row["source_name"], row["boundary_code"]) == (
        "鹿児島県", "鹿児島十島村中之島徳之尾", "774",
    )
    renderer = MapRenderer(mapping, boundaries, boundaries, boundaries)
    assert renderer._regions(551, {"points": [{
        "isArea": False, "pref": "鹿児島県",
        "addr": "鹿児島十島村中之島徳之尾", "scale": 45,
    }]}, {}) == {"774": (SCALE_RGB[45], "震度5弱")}
    before = mapping.read_bytes()
    assert update_mapping(source, mapping, boundaries) == 0
    assert mapping.read_bytes() == before


def test_rejects_gis_mismatch_without_changing_csv(tmp_path: Path) -> None:
    source, mapping, boundaries = _fixtures(tmp_path)
    stations = json.loads(source.read_text(encoding="utf-8"))
    stations[0]["area"]["name"] = "不一致"
    source.write_text(json.dumps(stations, ensure_ascii=False), encoding="utf-8")
    before = mapping.read_bytes()
    with pytest.raises(ValueError, match="does not match GIS"):
        update_mapping(source, mapping, boundaries)
    assert mapping.read_bytes() == before


def test_rejects_conflicting_existing_mapping(tmp_path: Path) -> None:
    source, mapping, boundaries = _fixtures(tmp_path)
    update_mapping(source, mapping, boundaries)
    text = mapping.read_text(encoding="utf-8-sig").replace(",774,", ",770,")
    mapping.write_text(text, encoding="utf-8-sig")
    before = mapping.read_bytes()
    with pytest.raises(ValueError, match="conflicts with existing mapping"):
        update_mapping(source, mapping, boundaries)
    assert mapping.read_bytes() == before
