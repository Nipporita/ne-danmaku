"""Configuration loader for the standalone danmaku backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLACKLIST_PATH = PROJECT_ROOT / "assets_danmaku" / "blacklist.txt"


class SatoriConfig(BaseModel):
    host: str
    port: int
    path: str = "/"
    token: str
    group_map: dict[str, str]


class BilibiliConfig(BaseModel):
    room_ids: dict[int, str]
    sess_data: str


class UpstreamConfig(BaseModel):
    token: str


class DanmakuConfig(BaseModel):
    satori: Optional[SatoriConfig] = None
    bilibili: Optional[BilibiliConfig] = None
    upstream: Optional[UpstreamConfig] = None
    blacklists: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    danmaku: DanmakuConfig = Field(default_factory=DanmakuConfig)


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def load_config(config_path: str | Path = "config.json") -> AppConfig:
    config_file = resolve_path(config_path)

    if not config_file.exists():
        logger.warning("Config file {} not found, using defaults", config_file)
        config = AppConfig()
        config.danmaku.blacklists.extend(get_blacklist_patterns())
        return config

    try:
        with config_file.open(encoding="utf-8") as f:
            data = json.load(f)

        config = AppConfig(**data)
        config.danmaku.blacklists.extend(get_blacklist_patterns())
        logger.info("Loaded config from {}", config_file)
        logger.info("Loaded {} blacklist patterns", len(config.danmaku.blacklists))
        return config
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse config {}: {}", config_file, exc)
        config = AppConfig()
        config.danmaku.blacklists.extend(get_blacklist_patterns())
        return config


def get_blacklist_patterns(blacklist_file: str | Path = BLACKLIST_PATH) -> list[str]:
    path = resolve_path(blacklist_file)
    if not path.exists():
        logger.debug("Blacklist file {} not found", path)
        return []

    try:
        patterns: list[str] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        return patterns
    except Exception as exc:  # pragma: no cover - best effort logging
        logger.error("Failed to load blacklist file {}: {}", path, exc)
        return []


def save_config(config: AppConfig, config_path: str | Path = "config.json") -> bool:
    config_file = resolve_path(config_path)
    try:
        with config_file.open("w", encoding="utf-8") as f:
            json.dump(config.model_dump(exclude_none=True), f, indent=2, ensure_ascii=False)
        logger.info("Saved config to {}", config_file)
        return True
    except Exception as exc:  # pragma: no cover - best effort logging
        logger.error("Failed to save config {}: {}", config_file, exc)
        return False
