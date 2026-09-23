"""Watchdog-based config.json file watcher.

Follows the same pattern as ``watcher.py`` (blacklist file watching).
When the config file is modified, the callback is invoked with a
debounce guard to avoid double-fires from editor save patterns.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _ConfigFileHandler(FileSystemEventHandler):
    """Watchdog handler that invokes a callback when the config file changes."""

    def __init__(self, callback: Callable[[], None], config_path: Path):
        self._callback = callback
        self._config_path = config_path.resolve()
        self._last_trigger: float = 0.0

    @property
    def config_path(self) -> Path:
        """Expose the watched path for external reference."""
        return self._config_path

    def on_modified(self, event) -> None:
        path = Path(os.fsdecode(event.src_path)).resolve()
        if path != self._config_path:
            return

        # 500ms debounce to avoid double-fires from editor save patterns
        now = time.monotonic()
        if now - self._last_trigger < 0.5:
            return
        self._last_trigger = now

        logger.info("config.json changed on disk, triggering reload")
        try:
            self._callback()
        except Exception:
            logger.exception("Unhandled error during config reload callback")


def start_config_watcher(
    callback: Callable[[], None],
    config_path: Path,
) -> tuple[Observer, _ConfigFileHandler]:
    """Start a watchdog observer that monitors *config_path* for changes.

    Args:
        callback: Invoked (with no arguments) when the file is modified.
        config_path: Absolute path to the config JSON file.

    Returns:
        A ``(observer, handler)`` tuple — store these for cleanup on shutdown.
    """
    resolved = config_path.resolve()
    handler = _ConfigFileHandler(callback, resolved)
    observer = Observer()
    observer.schedule(
        handler,
        resolved.parent.as_posix(),
        recursive=False,
    )
    observer.start()
    logger.info("Config file watcher started for {}", resolved)
    return observer, handler
