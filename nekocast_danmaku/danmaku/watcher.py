from __future__ import annotations

from pathlib import Path
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.events import FileSystemEventHandler

from loguru import logger

from .models import BlacklistService

class _BlacklistFileHandler(FileSystemEventHandler):
    def __init__(
        self,
        service: BlacklistService,
        pattern_file: Path,
        user_file: Path,
    ):
        self.service = service
        self.pattern_file = pattern_file
        self.user_file = user_file

    def on_modified(self, event):
        path = Path(event.src_path)

        if path == self.pattern_file or path == self.user_file:
            logger.info("Blacklist file changed: {}", path.name)
            self.service.reload(self.pattern_file, self.user_file)


def start_blacklist_watcher(
    service: BlacklistService,
    pattern_file: Path,
    user_file: Path,
) -> None:
    handler = _BlacklistFileHandler(service, pattern_file, user_file)

    observer = Observer()
    observer.schedule(handler, pattern_file.parent, recursive=False)
    observer.start()

    logger.info("Started blacklist watcher")
    
    service.watchdog = observer