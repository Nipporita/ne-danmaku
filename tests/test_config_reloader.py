"""ConfigReloader: hot-swap semantics, rejection paths, and notification."""

from __future__ import annotations

import asyncio

from nekocast_danmaku.config import load_config
from nekocast_danmaku.danmaku.config_reloader import ConfigReloader


class _RecordingManager:
    """Minimal stand-in for ConnectionManager — just records broadcast payloads."""

    def __init__(self):
        self.frames: list[dict] = []

    async def broadcast_config(self, payload: dict) -> None:
        self.frames.append(payload)


# --------------------------------------------------------------------------
# reload()
# --------------------------------------------------------------------------

def test_reload_applies_hot_fields_and_preserves_identity(
    config_file, raw_config, app_state, write_config
):
    """``create_router`` closes over ``config.danmaku`` at startup, so the live
    object must be mutated in place rather than replaced."""
    live_danmaku = app_state.config.danmaku
    reloader = ConfigReloader(app_state)

    raw_config["danmaku"]["max_message_length"] = 20
    raw_config["danmaku"]["superchat"]["default_cost"] = 99.0
    write_config(config_file, raw_config)

    result = reloader.reload(config_file)

    assert result["changed"] == {"max_message_length", "superchat"}
    assert app_state.config.danmaku is live_danmaku
    assert live_danmaku.max_message_length == 20
    assert live_danmaku.superchat.default_cost == 99.0
    assert app_state.config_reload_counter == 1


def test_reload_updates_upstream_token_in_place(
    config_file, raw_config, app_state, write_config
):
    """``validate_admin_token`` reads ``config.upstream.token`` per request,
    so a reloaded token must be visible through the same object."""
    upstream = app_state.config.danmaku.upstream
    reloader = ConfigReloader(app_state)

    raw_config["danmaku"]["upstream"]["token"] = "token-2"
    write_config(config_file, raw_config)
    reloader.reload(config_file)

    assert app_state.config.danmaku.upstream is upstream
    assert upstream.token == "token-2"


def test_reload_ignores_non_hot_reloadable_fields(
    config_file, raw_config, app_state, write_config
):
    reloader = ConfigReloader(app_state)

    raw_config["port"] = 9999
    write_config(config_file, raw_config)
    result = reloader.reload(config_file)

    assert result["changed"] == set()
    assert app_state.config.port == 8000
    assert app_state.config_reload_counter == 0


def test_reload_bad_json_leaves_running_config_intact(
    config_file, app_state
):
    reloader = ConfigReloader(app_state)
    config_file.write_text("{ broken", encoding="utf-8")

    result = reloader.reload(config_file)

    assert result["changed"] == set()
    assert app_state.config.danmaku.max_message_length == 50
    assert app_state.config.danmaku.upstream.token == "token-1"
    assert app_state.config_reload_counter == 0


def test_reload_missing_file_is_a_noop(tmp_path, app_state):
    reloader = ConfigReloader(app_state)

    result = reloader.reload(tmp_path / "absent.json")

    assert result["changed"] == set()
    assert app_state.config_reload_counter == 0


def test_reload_invalid_value_keeps_old_config(
    config_file, raw_config, app_state, write_config
):
    reloader = ConfigReloader(app_state)

    raw_config["danmaku"]["max_message_length"] = "not-an-int"
    write_config(config_file, raw_config)

    result = reloader.reload(config_file)

    assert result["changed"] == set()
    assert app_state.config.danmaku.max_message_length == 50


# --------------------------------------------------------------------------
# apply_updates()
# --------------------------------------------------------------------------

def test_apply_updates_rejects_invalid_value(app_state):
    reloader = ConfigReloader(app_state)

    result = reloader.apply_updates({"max_message_length": "not-an-int"})

    assert result["ok"] is False
    assert "error" in result
    assert app_state.config.danmaku.max_message_length == 50


def test_apply_updates_ignores_non_hot_fields(app_state):
    reloader = ConfigReloader(app_state)

    result = reloader.apply_updates({"port": 9999})

    assert result["ok"] is True
    assert result["changes"] == []
    assert app_state.config.port == 8000


def test_apply_updates_nested_dotted_path(app_state):
    reloader = ConfigReloader(app_state)

    result = reloader.apply_updates({"cash.initial_huo": 7.5})

    assert result["ok"] is True
    assert result["changes"] == ["cash.initial_huo"]
    assert app_state.config.danmaku.cash.initial_huo == 7.5


def test_apply_updates_persist_round_trip(config_file, app_state):
    """Regression: save_config rejected Path fields, so persist=True always
    came back with a persist_error instead of writing the file."""
    reloader = ConfigReloader(app_state)

    result = reloader.apply_updates(
        {"max_message_length": 33}, persist=True, config_path=config_file
    )

    assert result["ok"] is True
    assert result["persisted"] is True
    assert "persist_error" not in result
    assert load_config(config_file).danmaku.max_message_length == 33


# --------------------------------------------------------------------------
# consumer notification
# --------------------------------------------------------------------------

def test_reload_broadcasts_config_frame(
    config_file, raw_config, app_state, write_config
):
    async def scenario():
        manager = _RecordingManager()
        app_state.danmaku_manager = manager
        # constructed inside the loop so the reloader captures it
        reloader = ConfigReloader(app_state)

        raw_config["danmaku"]["max_message_length"] = 41
        write_config(config_file, raw_config)
        reloader.reload(config_file)

        # the broadcast is scheduled cross-thread; give it a moment to land
        for _ in range(100):
            if manager.frames:
                break
            await asyncio.sleep(0.01)
        return manager.frames

    frames = asyncio.run(scenario())

    assert len(frames) == 1
    assert frames[0]["type"] == "config"
    assert frames[0]["max_message_length"] == 41
    assert frames[0]["config_version"] == 1


def test_construction_without_running_loop_skips_broadcast(
    config_file, raw_config, app_state, write_config
):
    """Sync construction (tests, scripts) must not explode — it just skips it."""
    app_state.danmaku_manager = _RecordingManager()
    reloader = ConfigReloader(app_state)

    assert reloader._loop is None

    raw_config["danmaku"]["max_message_length"] = 42
    write_config(config_file, raw_config)

    assert reloader.reload(config_file)["changed"] == {"max_message_length"}


def test_no_broadcast_when_nothing_changed(config_file, app_state):
    async def scenario():
        manager = _RecordingManager()
        app_state.danmaku_manager = manager
        reloader = ConfigReloader(app_state)

        reloader.reload(config_file)  # unchanged on-disk copy
        await asyncio.sleep(0.05)
        return manager.frames

    assert asyncio.run(scenario()) == []
