from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from math import isfinite
from pathlib import Path


@dataclass(frozen=True)
class Config:
    threshold: int = 40
    log_level: str = "INFO"
    database: Path = Path("/var/lib/awoke/awoke.sqlite3")
    area_mapping: Path = Path("/opt/awoke/share/area_mapping.csv")
    seismic_shapefile: Path = Path("/opt/awoke/share/20240520_AreaForecastLocalE_GIS/地震情報／細分区域.shp")
    tsunami_shapefile: Path = Path("/opt/awoke/share/20240520_AreaTsunami_GIS/津波予報区.shp")
    land_shapefile: Path = Path("/opt/awoke/share/ne_10m_land/ne_10m_land.shp")
    event_match_radius_km: float = 100.0
    retention_days: int = 7
    reconnect_min_seconds: float = 2.0
    reconnect_max_seconds: float = 60.0

    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        section = values.get("awoke", values)
        if not isinstance(section, dict):
            raise ValueError("Configuration must be a TOML table")
        allowed = {field for field in cls.__dataclass_fields__}
        unknown = set(section) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        converted = dict(section)
        for key in ("database", "area_mapping", "seismic_shapefile", "tsunami_shapefile", "land_shapefile"):
            if key in converted:
                converted[key] = Path(converted[key])
        config = cls(**converted)
        if (
            not isinstance(config.threshold, int)
            or isinstance(config.threshold, bool)
            or config.threshold not in (10, 20, 30, 40, 45, 50, 55, 60, 70)
        ):
            raise ValueError("threshold must be a P2P maxScale value from 10,20,30,40,45,50,55,60,70")
        if not isinstance(config.retention_days, int) or isinstance(config.retention_days, bool) or config.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if not _positive_number(config.event_match_radius_km):
            raise ValueError("event_match_radius_km must be positive")
        if not isinstance(config.log_level, str) or config.log_level.upper() not in (
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
        ):
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        if not _positive_number(config.reconnect_min_seconds):
            raise ValueError("reconnect_min_seconds must be positive")
        if (
            not _positive_number(config.reconnect_max_seconds)
            or config.reconnect_max_seconds < config.reconnect_min_seconds
        ):
            raise ValueError("reconnect_max_seconds must be at least reconnect_min_seconds")
        return config


def _positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0


def webhook_credential() -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RuntimeError("CREDENTIALS_DIRECTORY is not set; start through systemd LoadCredential")
    path = Path(directory) / "discord-webhook-url"
    value = path.read_text(encoding="utf-8").strip()
    if not value.startswith("https://discord.com/api/webhooks/"):
        raise ValueError("Discord webhook credential has an unexpected URL format")
    return value
