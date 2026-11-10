from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from awoke.config import Config
from tools import create_release


def _release_source(root: Path) -> None:
    (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (root / "README.md").write_text("test", encoding="utf-8")
    (root / "config.example.toml").write_text("[awoke]\n", encoding="utf-8")
    for path in ("data", "systemd", "src", *create_release.ASSET_DIRS):
        (root / path).mkdir()
    (root / "data" / "area_mapping.csv").write_text("test", encoding="utf-8")
    (root / "systemd" / "awoke.service").write_text("test", encoding="utf-8")
    (root / "src" / "module.py").write_text("pass", encoding="utf-8")
    for folder in create_release.ASSET_DIRS:
        for suffix in (".shp", ".shx", ".dbf"):
            (root / folder / f"地図{suffix}").write_bytes(b"test")


def test_release_has_ascii_names_matching_default_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _release_source(root)
    monkeypatch.setattr(create_release, "ROOT", root)
    archive_path = create_release.create_release(tmp_path / "output")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert all(name.isascii() for name in names)
        assert "awoke-1.2.3/share/20240520_AreaForecastLocalE_GIS/seismic.shp" in names
        assert "awoke-1.2.3/share/20240520_AreaTsunami_GIS/tsunami.dbf" in names
        assert not any("/ne_10m_land/" in name for name in names)
        assert all(stat.S_IMODE(info.external_attr >> 16) == 0o644 for info in archive.infolist())
    assert Config().seismic_shapefile.name == "seismic.shp"
    assert Config().tsunami_shapefile.name == "tsunami.shp"


def test_release_rejects_missing_shapefile_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _release_source(root)
    (root / "20240520_AreaTsunami_GIS" / "地図.dbf").unlink()
    monkeypatch.setattr(create_release, "ROOT", root)
    with pytest.raises(FileNotFoundError, match=r"\.dbf"):
        create_release.create_release(tmp_path / "output")
