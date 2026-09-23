"""save_config / load_config round-trip and failure modes."""

from __future__ import annotations

import json

import pytest

from nekocast_danmaku.config import PROJECT_ROOT, AppConfig, load_config, save_config


def test_save_config_serialises_path_fields(tmp_path):
    """Regression: model_dump(mode="python") leaves Path objects behind and
    json.dump then raises "Object of type WindowsPath is not JSON serializable"."""
    cfg = AppConfig(
        **{"danmaku": {"asset_dir": "assets_danmaku", "upstream": {"token": "t"}}}
    )
    out = tmp_path / "out.json"

    assert save_config(cfg, out) is True

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["danmaku"]["asset_dir"] == "assets_danmaku"


def test_save_config_round_trips(tmp_path):
    cfg = AppConfig(
        **{
            "danmaku": {
                "max_message_length": 33,
                "upstream": {"token": "tok"},
                "cash": {"secret_key": "s"},
            }
        }
    )
    out = tmp_path / "out.json"
    assert save_config(cfg, out) is True

    back = load_config(out)
    assert back.danmaku.max_message_length == 33
    assert back.danmaku.upstream.token == "tok"


def test_load_config_returns_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path / "absent.json")
    assert cfg.danmaku.max_message_length == 50


def test_load_config_raises_on_malformed_existing_file(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{ not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="failed to parse"):
        load_config(bad)


def test_load_config_raises_on_invalid_field(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text(
        json.dumps({"danmaku": {"max_message_length": "nope"}}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="failed to parse"):
        load_config(bad)


def test_example_config_is_parseable():
    example = PROJECT_ROOT / "config.example.json"
    cfg = AppConfig(**json.loads(example.read_text(encoding="utf-8")))
    assert cfg.danmaku.max_message_length > 0
