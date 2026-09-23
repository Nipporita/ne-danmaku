"""Config hot-reload orchestrator.

Listens for config.json changes (via watchdog) or admin API calls,
re-validates the full file through Pydantic, and selectively
mutates only hot-reloadable fields in-place on the running config object.

Consumers that cache config values at startup (module globals,
constructor parameters) are notified so they can pick up new values.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from ..config import AppConfig, DanmakuConfig, save_config


# =========================
# Hot-reloadable field paths (dotted paths into DanmakuConfig)
# =========================

SIMPLE_FIELDS: set[str] = {
    "max_message_length",
    "dedup_window",
}

NESTED_FIELDS: set[str] = {
    "upstream.token",
}

# Entire subtrees — replace the whole object
SUBTREE_FIELDS: set[str] = {
    "superchat",
    "gift",
}

CASH_FIELDS: set[str] = {
    "cash.enabled",
    "cash.initial_huo",
    "cash.reward_huo_per_message",
    "cash.reward_huo_interval_seconds",
    "cash.reward_huo_per_interval",
    "cash.initial_yuan",
    "cash.reward_yuan_per_message",
    "cash.reward_yuan_interval_seconds",
    "cash.reward_yuan_per_interval",
}

# Also allow hot-reloading group maps via dict mutation
DICT_MUTATION_FIELDS: set[str] = {
    "satori.group_map",
    "onebot_v11.group_map",
}

HOT_RELOADABLE_FIELDS = (
    SIMPLE_FIELDS | NESTED_FIELDS | SUBTREE_FIELDS | CASH_FIELDS | DICT_MUTATION_FIELDS
)


# =========================
# Helpers
# =========================

def _deep_get(obj: object, dotted: str) -> Any:
    """Get a nested attribute by dotted path (e.g. ``'upstream.token'``).

    Returns ``None`` if any intermediate attribute is ``None``.
    """
    for part in dotted.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part)
    return obj


def _deep_set(obj: object, dotted: str, value: Any) -> None:
    """Set a nested attribute by dotted path, mutating intermediates in-place.

    No-op if any intermediate attribute is ``None``.
    """
    parts = dotted.split(".")
    for part in parts[:-1]:
        if obj is None:
            return
        obj = getattr(obj, part)
    if obj is not None:
        setattr(obj, parts[-1], value)


def _dict_replace(dct: dict, new: dict) -> None:
    """Replace contents of *dct* with *new* in-place (mutates, doesn't rebind)."""
    dct.clear()
    dct.update(new)


# =========================
# ConfigReloader
# =========================

class ConfigReloader:
    """Orchestrates config.json hot-reload into the running application.

    Stores a reference to ``app.state`` so it can reach all components
    that need notification when configuration changes.
    """

    def __init__(self, app_state: Any) -> None:
        import threading

        self.app_state = app_state
        self._lock = threading.Lock()

        # 广播配置变更需要事件循环句柄：reload() 由 watchdog 观察者线程调用，
        # 必须靠 run_coroutine_threadsafe 跨线程调度回事件循环。
        # 构造点在 startup_danmaku 内（事件循环中），能拿到 running loop；
        # 纯同步环境（如单元测试）下为 None，此时跳过广播。
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def reload(self, config_path: str | Path) -> dict[str, set[str]]:
        """Re-read *config_path*, validate, and apply hot-reloadable changes.

        Called by the watchdog thread when ``config.json`` is modified.
        Returns a dict of ``{"changed": {...}, "applied": {...}}`` for logging.
        """
        config_path = Path(config_path)
        logger.info("Config file change detected, reloading from {}", config_path)

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read config file {}: {}", config_path, exc)
            return {"changed": set(), "applied": set()}

        try:
            validated = AppConfig(**raw)
        except Exception as exc:
            logger.error("Config validation failed, keeping old config: {}", exc)
            return {"changed": set(), "applied": set()}

        new_dc = validated.danmaku
        current_dc: DanmakuConfig = self.app_state.config.danmaku

        with self._lock:
            changed = self._diff_and_mutate(current_dc, new_dc)

            if changed:
                # 先自增版本号再通知：广播帧里带的 config_version 必须已经是新的
                self.app_state.config_reload_counter = getattr(
                    self.app_state, "config_reload_counter", 0
                ) + 1
                self._notify_consumers(changed)

        if changed:
            logger.info("Config reloaded successfully — applied: {}", sorted(changed))
        else:
            logger.debug("Config file changed but no hot-reloadable values differ")

        return {"changed": changed, "applied": changed}

    def apply_updates(
        self,
        updates: dict[str, Any],
        *,
        persist: bool = False,
        config_path: str | Path = "config.json",
    ) -> dict[str, Any]:
        """Apply partial config updates from an admin API call.

        *updates* is a flat-ish dict keyed by dotted path; the subset of
        keys in ``HOT_RELOADABLE_FIELDS`` are validated and applied.
        If *persist* is true the full running config is written back to
        the file on disk.
        """
        from ..config import resolve_path

        current_dc: DanmakuConfig = self.app_state.config.danmaku

        # Build a full raw dict from the current running config, then overlay updates
        current_raw = self.app_state.config.model_dump(exclude_none=True)
        # unwrap danmaku
        danmaku_raw = current_raw.get("danmaku", {})
        self._apply_updates_to_dict(danmaku_raw, updates)

        try:
            validated = AppConfig(**current_raw)
        except Exception as exc:
            logger.error("Partial config update validation failed: {}", exc)
            return {"ok": False, "error": str(exc)}

        new_dc = validated.danmaku
        with self._lock:
            changed = self._diff_and_mutate(current_dc, new_dc)

            if changed:
                # 先自增版本号再通知：广播帧里带的 config_version 必须已经是新的
                self.app_state.config_reload_counter = getattr(
                    self.app_state, "config_reload_counter", 0
                ) + 1
                self._notify_consumers(changed)

        if persist:
            config_file = resolve_path(config_path)
            try:
                save_config(self.app_state.config, config_file)
                logger.info("Config persisted to {}", config_file)
            except Exception as exc:
                logger.error("Failed to persist config: {}", exc)
                return {"ok": True, "changes": sorted(changed), "persist_error": str(exc)}

        return {"ok": True, "changes": sorted(changed), "persisted": persist}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_updates_to_dict(self, target: dict, updates: dict[str, Any]) -> None:
        """Apply dotted-path updates into a nested dict in-place."""
        for key, value in updates.items():
            if key not in HOT_RELOADABLE_FIELDS:
                continue
            parts = key.split(".")
            node = target
            for part in parts[:-1]:
                if part not in node:
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value

    def _diff_and_mutate(
        self, current: DanmakuConfig, new: DanmakuConfig
    ) -> set[str]:
        """Compare *current* (live) and *new* (validated) configs.

        Mutates *current* in-place for every differing field in
        ``HOT_RELOADABLE_FIELDS``. Returns the set of changed field paths.
        """
        changed: set[str] = set()

        for field in SIMPLE_FIELDS:
            old_val = getattr(current, field)
            new_val = getattr(new, field)
            if old_val != new_val:
                setattr(current, field, new_val)
                changed.add(field)

        for field in NESTED_FIELDS:
            old_val = _deep_get(current, field)
            new_val = _deep_get(new, field)
            if old_val != new_val:
                _deep_set(current, field, new_val)
                changed.add(field)

        for field in SUBTREE_FIELDS:
            old_obj = getattr(current, field)
            new_obj = getattr(new, field)
            if old_obj != new_obj:
                setattr(current, field, new_obj)
                changed.add(field)

        for field in CASH_FIELDS:
            old_val = _deep_get(current, field)
            new_val = _deep_get(new, field)
            if old_val != new_val:
                _deep_set(current, field, new_val)
                changed.add(field)

        for field in DICT_MUTATION_FIELDS:
            old_val = _deep_get(current, field)
            new_val = _deep_get(new, field)
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                if old_val != new_val:
                    _dict_replace(old_val, new_val)
                    changed.add(field)

        return changed

    def _notify_consumers(self, changed_keys: set[str]) -> None:
        """Push config changes to cached consumers."""
        dc: DanmakuConfig = self.app_state.config.danmaku
        cm = getattr(self.app_state, "danmaku_manager", None)
        cs = getattr(self.app_state, "room_cash_system", None)

        # -- max_message_length -> ConnectionManager
        if "max_message_length" in changed_keys and cm is not None:
            cm.max_message_length = dc.max_message_length
            logger.info("[config-reload] ConnectionManager.max_message_length = {}", dc.max_message_length)

        # -- dedup_window -> DanmakuFilter
        if "dedup_window" in changed_keys and cm is not None and cm.danmaku_filter is not None:
            cm.danmaku_filter.dedup_window = dc.dedup_window
            logger.info("[config-reload] DanmakuFilter.dedup_window = {}", dc.dedup_window)

        # -- superchat / gift -> danmaku_builder module globals
        sc_changed = "superchat" in changed_keys
        gift_changed = "gift" in changed_keys
        if sc_changed or gift_changed:
            from .danmaku_class.danmaku_builder import configure_parsing_rules

            configure_parsing_rules(
                superchat=dc.superchat,
                gift=dc.gift,
            )
            logger.info(
                "[config-reload] reconfigured parsing rules (superchat={}, gift={})",
                sc_changed,
                gift_changed,
            )

        # -- cash.* -> RoomCashSystem.policy
        cash_keys = {k for k in changed_keys if k.startswith("cash.")}
        if cash_keys and cs is not None:
            safe_cash_fields = {
                "enabled",
                "initial_huo",
                "reward_huo_per_message",
                "reward_huo_interval_seconds",
                "reward_huo_per_interval",
                "initial_yuan",
                "reward_yuan_per_message",
                "reward_yuan_interval_seconds",
                "reward_yuan_per_interval",
            }
            updates = {}
            for ck in cash_keys:
                attr_name = ck.split(".", 1)[1]  # "cash.enabled" -> "enabled"
                if attr_name in safe_cash_fields:
                    updates[attr_name] = getattr(dc.cash, attr_name)
            if updates:
                try:
                    updated = cs.update_policy(**updates)
                    logger.info("[config-reload] CashPolicy updated: {}", updated)
                except Exception as exc:
                    logger.error("[config-reload] CashPolicy update failed: {}", exc)

        # -- 广播配置版本给所有前端（前端据此刷新 maxlength 等派生值）
        self._broadcast_to_clients()

    def _broadcast_to_clients(self) -> None:
        """把配置版本推给所有已连接的客户端。

        可能在 watchdog 线程（``reload``）或事件循环线程（``apply_updates``）
        上被调用，``run_coroutine_threadsafe`` 两种情况都安全。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        cm = getattr(self.app_state, "danmaku_manager", None)
        if cm is None:
            return

        dc: DanmakuConfig = self.app_state.config.danmaku
        payload = {
            "type": "config",
            "config_version": getattr(self.app_state, "config_reload_counter", 0),
            "max_message_length": dc.max_message_length,
        }

        try:
            future = asyncio.run_coroutine_threadsafe(cm.broadcast_config(payload), loop)
        except Exception as exc:
            logger.error("[config-reload] failed to schedule config broadcast: {}", exc)
            return

        def _log_failure(fut) -> None:
            # 不 await 这个 future，异常必须显式捞，否则会被静默吞掉
            exc = fut.exception()
            if exc is not None:
                logger.error("[config-reload] config broadcast failed: {}", exc)

        future.add_done_callback(_log_failure)
