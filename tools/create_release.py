from __future__ import annotations

import argparse
import stat
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHAPE_SUFFIXES = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
ASSET_DIRS = {
    "20240520_AreaForecastLocalE_GIS": "seismic",
    "20240520_AreaTsunami_GIS": "tsunami",
}


def create_release(destination: Path) -> Path:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    archive_path = destination / f"awoke-{version}.zip"
    prefix = f"awoke-{version}"
    included = [
        (ROOT / "pyproject.toml", "pyproject.toml"),
        (ROOT / "README.md", "README.md"),
        (ROOT / "config.example.toml", "config.example.toml"),
        (ROOT / "systemd" / "awoke.service", "systemd/awoke.service"),
        (ROOT / "data" / "area_mapping.csv", "share/area_mapping.csv"),
    ]
    for folder_name, archive_stem in ASSET_DIRS.items():
        folder = ROOT / folder_name
        shapes = [item for item in folder.iterdir() if item.is_file() and item.suffix.lower() == ".shp"]
        if len(shapes) != 1:
            raise ValueError(f"Expected exactly one shapefile in {folder}, found {len(shapes)}")
        shape = shapes[0]
        files = [
            item for item in folder.iterdir()
            if item.is_file() and item.stem == shape.stem and item.suffix.lower() in SHAPE_SUFFIXES
        ]
        missing = {".shp", ".shx", ".dbf"} - {item.suffix.lower() for item in files}
        if missing:
            raise FileNotFoundError(f"Missing shapefile components in {folder}: {', '.join(sorted(missing))}")
        included.extend((path, f"share/{folder_name}/{archive_stem}{path.suffix.lower()}") for path in files)
    included.extend(
        (path, path.relative_to(ROOT).as_posix())
        for path in (ROOT / "src").rglob("*.py")
    )
    if not (ROOT / "data" / "area_mapping.csv").is_file():
        raise FileNotFoundError("Missing data/area_mapping.csv")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, archive_name in included:
            name = f"{prefix}/{archive_name}"
            archive.write(path, name)
            info = archive.getinfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a source-and-map-data release archive")
    parser.add_argument("destination", type=Path, help="directory where the ZIP archive will be written")
    args = parser.parse_args()
    result = create_release(args.destination)
    print(result)


if __name__ == "__main__":
    main()
