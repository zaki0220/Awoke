from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import shapefile

FIELDS = (
    "code", "source_name", "prefecture", "source_granularity", "municipality",
    "boundary_name", "boundary_code", "match_method", "status", "sample_file",
)


def update_mapping(source: Path, mapping: Path, boundaries: Path) -> int:
    with source.open(encoding="utf-8-sig") as stream:
        stations: Any = json.load(stream)
    if not isinstance(stations, list):
        raise ValueError("Station data must be a JSON array")
    with mapping.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("Unexpected mapping CSV columns")
        rows = list(reader)
    with shapefile.Reader(str(boundaries), encoding="utf-8") as reader:
        names = {str(record["code"]): str(record["name"]) for record in reader.iterRecords(fields=["code", "name"])}

    existing: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if row["code"] == "551" and row["source_granularity"] == "observation_point":
            key = row["prefecture"], row["source_name"]
            if key in existing:
                raise ValueError(f"Duplicate existing station: {key}")
            existing[key] = row

    added = 0
    seen: set[tuple[str, str]] = set()
    for station in stations:
        if not isinstance(station, dict):
            raise ValueError("Invalid station entry")
        try:
            prefecture, name = station["pref"]["name"], station["name"]
            area_code, area_name = station["area"]["code"], station["area"]["name"]
            municipality = station["city"]["name"]
        except (KeyError, TypeError) as error:
            raise ValueError("Station is missing required fields") from error
        if not all(isinstance(value, str) and value for value in (prefecture, name, area_code, area_name, municipality)):
            raise ValueError("Station fields must be non-empty strings")
        key = prefecture, name
        if key in seen:
            raise ValueError(f"Duplicate source station: {key}")
        seen.add(key)
        if names.get(area_code) != area_name:
            raise ValueError(f"Station area does not match GIS: {key} {area_code} {area_name}")
        old = existing.get(key)
        if old is not None:
            if old["status"] != "mapped" or old["boundary_code"] != area_code or old["boundary_name"] != area_name:
                raise ValueError(f"Station conflicts with existing mapping: {key}")
            continue
        rows.append({
            "code": "551", "source_name": name, "prefecture": prefecture,
            "source_granularity": "observation_point", "municipality": municipality,
            "boundary_name": area_name, "boundary_code": area_code,
            "match_method": "provided station area code and name -> exact GIS match",
            "status": "mapped", "sample_file": source.name,
        })
        added += 1
    if added:
        with mapping.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description="Add validated station mappings from JSON")
    parser.add_argument("source", type=Path, help="station JSON file")
    parser.add_argument("mapping", type=Path, help="existing mapping CSV to update")
    parser.add_argument("boundaries", type=Path, help="seismic area shapefile")
    args = parser.parse_args()
    print(f"Added {update_mapping(args.source, args.mapping, args.boundaries)} station mappings")


if __name__ == "__main__":
    main()
