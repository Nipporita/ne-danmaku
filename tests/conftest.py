"""Shared fixtures for the test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nekocast_danmaku.config import AppConfig


BASE_RAW: dict = {
    "host": "0.0.0.0",
    "port": 8000,
    "danmaku": {
        "upstream": {"token": "token-1"},
        "superchat": {
            "default_cost": 10.0,
            "duration_per_cost": 1.0,
            "min_duration": 10,
            "max_duration": 300,
        },
        "gift": {"default_cost": 1.0, "items": {}},
        "cash": {
            "enabled": True,
            "initial_huo": 0.0,
            "initial_yuan": 100.0,
            "secret_key": "s",
        },
        "dedup_window": 5,
        "max_message_length": 50,
    },
}


@pytest.fixture
def raw_config() -> dict:
    """A fresh mutable copy of the base config payload."""
    return json.loads(json.dumps(BASE_RAW))


@pytest.fixture
def write_config():
    """Return a helper that dumps *raw* to *path* as JSON."""

    def _write(path: Path, raw: dict) -> Path:
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def config_file(tmp_path, raw_config, write_config) -> Path:
    return write_config(tmp_path / "config.json", raw_config)


class FakeState:
    """Stands in for ``app.state`` — only the attributes the reloader touches.

    ``danmaku_manager`` / ``room_cash_system`` are intentionally absent so the
    ``getattr(..., None)`` lookups in ``ConfigReloader`` behave as they would
    when those subsystems are not wired up.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.config_reload_counter = 0


@pytest.fixture
def app_state(config_file) -> FakeState:
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    return FakeState(AppConfig(**raw))
