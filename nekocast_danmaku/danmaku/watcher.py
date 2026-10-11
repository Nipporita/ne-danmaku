from __future__ import annotations

import os
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from loguru import logger

from .models import BlacklistService

class _BlacklistFileHandler(FileSystemEventHandler):
    """
    处理黑名单文件的文件系统事件。

    参数:
        service (BlacklistService): 黑名单服务实例。
        pattern_file (Path): 模式文件的路径。
        user_file (Path): 用户文件的路径。
        extra_files (Iterable[Path]): 额外的词表文件（第三方词库，submodule 那种）。
    """

    def __init__(
        self,
        service: BlacklistService,
        pattern_file: Path,
        user_file: Path,
        extra_files=(),
    ):
        self.service = service  # 初始化黑名单服务
        self.pattern_file = pattern_file  # 初始化模式文件路径
        self.user_file = user_file  # 初始化用户文件路径
        # 额外词表：submodule 更新（git submodule update --remote）后落盘即自动重载
        self.extra_files = {Path(p) for p in (extra_files or [])}
        self.watched_files = {Path(pattern_file), Path(user_file)} | self.extra_files

    def on_modified(self, event):
        """
        当监视的文件被修改时调用。

        参数:
            event (FileSystemEvent): 文件系统事件对象。
        """
        path = Path(os.fsdecode(event.src_path))  # 获取被修改的文件路径

        # 检查被修改的文件是否为黑名单文件之一
        if path in self.watched_files:
            logger.info("Blacklist file changed: {}", path.name)  # 记录文件修改日志
            self.service.reload(self.pattern_file, self.user_file)  # 重新加载黑名单文件


def start_blacklist_watcher(
    service: BlacklistService,
    pattern_file: Path,
    user_file: Path,
    extra_files=(),
) -> None:
    handler = _BlacklistFileHandler(service, pattern_file, user_file, extra_files)

    # 主文件和各份第三方词表可能在不同目录（词表通常在 blacklist/ 的 submodule 里），
    # 所以逐个目录挂监视器，而不是只盯 pattern_file 的父目录
    directories = {Path(f).parent for f in handler.watched_files if Path(f).parent.exists()}

    observer = Observer()
    for directory in sorted(directories):
        observer.schedule(handler, directory.as_posix(), recursive=False)
    observer.start()

    logger.info("Started blacklist watcher on {} dir(s)", len(directories))

    service.watchdog = observer
    service.handler = handler
