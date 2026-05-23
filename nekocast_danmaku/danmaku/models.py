"""弹幕数据模型
负责：
- 弹幕数据结构定义（Pydantic）
- 弹幕过滤（黑名单 / 去重）
- WebSocket 连接管理与广播
"""

import json
import regex
import time
from collections import defaultdict, deque
from typing import Any, Optional
from pathlib import Path

from fastapi import WebSocket
from loguru import logger
from pydantic import BaseModel, model_validator

from .danmaku_class.danmaku_message import (
    DanmakuMessage,
    EmoteMessage,
    MultiEmoteMessage,
    GiftMessage,
    PlainDanmakuMessage,
    SuperChatMessage,
)

from ..emotes.resolver import EmoteResolver

# =========================
# 上游传输数据包
# =========================


class DanmakuPacket(BaseModel):
    """上游弹幕数据包结构

    一个包只能是一条弹幕。
    """

    group: str  # 弹幕分组 / 频道
    danmaku: DanmakuMessage

class RoomSettings(BaseModel):
    """每个房间的动态弹幕设置。"""

    overlay_opacity: float = 100.0
    enable_external_emoji: bool = True
    enable_internal_emoji: bool = True
    enable_superchat: bool = True
    enable_gift: bool = True
    bind_position: bool = True

    @model_validator(mode="after")
    def clamp_values(self):
        self.overlay_opacity = max(0.0, min(100.0, self.overlay_opacity))
        return self


class RoomSettingsService:
    """管理每个 room 的可动态更新设置。

    如果提供了 ``room_db``，设置会同时持久化到 SQLite；
    否则退化为纯内存模式（重启丢失）。
    """

    def __init__(self, room_db=None):
        from .room_db import RoomDB

        self._db: RoomDB | None = room_db
        # 优先从数据库加载已有设置
        self._settings: dict[str, RoomSettings] = (
            self._db.load_all() if self._db else {}
        )

    def get(self, group: str) -> RoomSettings:
        return self._settings.get(group, RoomSettings())

    def update(self, group: str, settings: RoomSettings) -> RoomSettings:
        self._settings[group] = settings
        if self._db:
            self._db.save(group, settings)
        return settings


# =========================
# 弹幕过滤器
# =========================


class BlacklistService:
    """
    黑名单服务（只负责“是否应该被过滤”这一件事）

    功能：
    - 文本正则黑名单
    - 发送者 ID 黑名单
    """

    def __init__(self):
        # 已编译的正则
        self._patterns: list[regex.Pattern] = []
        self._pattern_strings: set[str] = set()  # 用于快速检查是否已存在某个模式字符串
        
        self._pattern_in_file: list[str] = []  # 维护一个原始字符串列表，保持与文件一致的顺序（用于持久化）

        # 禁止用户 ID
        self._forbidden_users: set[str] = set()
        self._forbidden_users_in_file: list[str] = []  # 维护一个原始用户 ID 列表，保持与文件一致的顺序（用于持久化）

        self.watchdog: Any = None  # 文件监视器（外部设置）
        
        self.handler: Any = None  # Watchdog 事件处理器（外部设置）

    # =========================
    # 加载 / 重载
    # =========================

    def load_patterns(self, path: Path) -> None:
        patterns = self._load_lines(path)

        compiled: list[regex.Pattern] = []
        for pat in patterns:
            try:
                compiled.append(regex.compile(pat, regex.IGNORECASE))
            except regex.error as exc:
                logger.error("Invalid blacklist regex '{}': {}", pat, exc)

        self._patterns = compiled
        self._pattern_strings = set(pat.pattern for pat in compiled)
        self._pattern_in_file = patterns  # 保持原始字符串列表
        logger.info("Loaded {} blacklist regex patterns", len(compiled))

    def load_users(self, path: Path) -> None:
        self._forbidden_users = set(self._load_lines(path))
        self._forbidden_users_in_file = list(self._forbidden_users)  # 保持原始用户 ID 列表
        logger.info("Loaded {} forbidden users", len(self._forbidden_users))

    def reload(self, pattern_path: Path, user_path: Path) -> None:
        self.load_patterns(pattern_path)
        self.load_users(user_path)
    
    def append_pattern(self, pattern: str, hard: bool = False) -> str:
        """动态追加一个黑名单正则模式"""
        if not hard:
            try:
                compiled = regex.compile(pattern, regex.IGNORECASE)
                self._patterns.append(compiled)
                self._pattern_strings.add(compiled.pattern)
                logger.info("Appended new blacklist pattern: {}", pattern)
                return f"Appended new blacklist pattern: {pattern}"
            except regex.error as exc:
                logger.error("Invalid regex pattern '{}': {}", pattern, exc)
                return f"Invalid regex pattern: {pattern}"
        else:
            # 直接追加到文件并重载（持久化）
            if self.watchdog and self.handler:
                pattern_path = self.handler.pattern_file
                try:
                    with open(pattern_path, "a", encoding="utf-8") as f:
                        f.write("\n" + pattern)
                    logger.info("Appended new blacklist pattern to file: {}", pattern)
                    return f"Appended new blacklist pattern to file: {pattern}"
                except Exception as exc:
                    logger.error("Failed to append pattern to file '{}': {}", pattern_path, exc)
                    return f"Failed to append pattern to file: {exc}"
            else:
                logger.warning("Cannot append pattern to file because watchdog is not set up")
                return "Cannot append pattern to file because watchdog is not set up, use append_pattern with hard=False instead\n" \
                    + self.append_pattern(pattern, hard=False)
    
    def remove_pattern(self, pattern: str, hard: bool = False) -> str:
        """动态移除一个黑名单正则模式"""
        if not hard:
            compiled = regex.compile(pattern, regex.IGNORECASE)
            before_count = len(self._patterns)
            self._patterns = [pat for pat in self._patterns if pat.pattern != compiled.pattern]
            self._pattern_strings.discard(compiled.pattern)
            after_count = len(self._patterns)
            logger.info("Removed blacklist pattern: {}, {} patterns remain", pattern, after_count)
            return f"Removed blacklist pattern: {pattern}, {after_count} patterns remain"
        else:
            # 从文件中移除并重载（持久化）
            if self.watchdog and self.handler:
                pattern_path = self.handler.pattern_file
                try:
                    lines = self._load_lines(pattern_path)
                    lines = [line for line in lines if line.strip() != pattern]
                    with open(pattern_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                    logger.info("Removed blacklist pattern from file: {}, {} patterns remain", pattern, len(self._patterns))
                    return f"Removed blacklist pattern from file: {pattern}, {len(self._patterns)} patterns remain"
                except Exception as exc:
                    logger.error("Failed to remove pattern from file '{}': {}", pattern_path, exc)
                    return f"Failed to remove pattern from file: {exc}"
            else:
                logger.warning("Cannot remove pattern from file because watchdog is not set up")
                return "Cannot remove pattern from file because watchdog is not set up, use remove_pattern with hard=False instead\n" \
                    + self.remove_pattern(pattern, hard=False)
    
    def ban_user(self, user_id: str, hard: bool = False) -> str:
        """动态禁止一个用户 ID"""
        if not hard:
            self._forbidden_users.add(user_id)
            logger.info("Banned user ID: {}", user_id)
            return f"Banned user ID: {user_id}"
        else:
            # 直接追加到文件并重载（持久化）
            if self.watchdog and self.handler:
                user_path = self.handler.user_file
                try:
                    with open(user_path, "a", encoding="utf-8") as f:
                        f.write(user_id + "\n")
                    logger.info("Banned user ID by appending to file: {}", user_id)
                    return f"Banned user ID by appending to file: {user_id}"
                except Exception as exc:
                    logger.error("Failed to ban user ID by appending to file '{}': {}", user_path, exc)
                    return f"Failed to ban user ID by appending to file: {exc}"
            else:
                logger.warning("Cannot ban user ID by appending to file because watchdog is not set up")
                return "Cannot ban user ID by appending to file because watchdog is not set up, use ban_user with hard=False instead\n" \
                    + self.ban_user(user_id, hard=False)
    
    def unban_user(self, user_id: str, hard: bool = False) -> str:
        """动态解除禁止一个用户 ID"""
        if not hard:
            self._forbidden_users.discard(user_id)
            logger.info("Unbanned user ID: {}", user_id)
            return f"Unbanned user ID: {user_id}"
        else:
            # 从文件中移除并重载（持久化）
            if self.watchdog and self.handler:
                user_path = self.handler.user_file
                try:
                    lines = self._load_lines(user_path)
                    lines = [line for line in lines if line.strip() != user_id]
                    with open(user_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                    logger.info("Unbanned user ID by removing from file: {}", user_id)
                    return f"Unbanned user ID by removing from file: {user_id}"
                except Exception as exc:
                    logger.error("Failed to unban user ID by removing from file '{}': {}", user_path, exc)
                    return f"Failed to unban user ID by removing from file: {exc}"
            else:
                logger.warning("Cannot unban user ID by removing from file because watchdog is not set up")
                return "Cannot unban user ID by removing from file because watchdog is not set up, use unban_user with hard=False instead\n" \
                + self.unban_user(user_id, hard=False)
    
    def list_patterns(self) -> list[dict[str, Any]]:
        """列出当前的黑名单正则模式"""
        """
        [
            {
                "pattern": "badword",
                "correct": bool (能否编译),
                "in_file": bool (是否在文件中),
                "in_memory": bool (是否在内存中),
            }
        ]
        """
        result_dict = {}
        for pat in self._pattern_strings:
            result_dict[pat] = {
                "pattern": pat,
                "correct": True,
                "in_file": pat in self._pattern_in_file,
                "in_memory": True,
            }
        
        for pat in self._pattern_in_file:
            if pat not in result_dict:
                correct = True
                try:
                    regex.compile(pat, regex.IGNORECASE)
                except regex.error:
                    correct = False
                    
                result_dict[pat] = {
                    "pattern": pat,
                    "correct": correct,
                    "in_file": True,
                    "in_memory": False,
                }
        
        return list(result_dict.values())
    
    def list_forbidden_users(self) -> list[dict[str, Any]]:
        """列出当前的禁止用户 ID"""
        """
        [
            {
                "user_id": "123456",
                "in_file": bool (是否在文件中),
                "in_memory": bool (是否在内存中),
            }
        ]
        """
        result_dict = {}
        for user_id in self._forbidden_users:
            result_dict[user_id] = {
                "user_id": user_id,
                "in_file": user_id in self._forbidden_users_in_file,
                "in_memory": True,
            }
        
        for user_id in self._forbidden_users_in_file:
            if user_id not in result_dict:
                result_dict[user_id] = {
                    "user_id": user_id,
                    "in_file": True,
                    "in_memory": False,
                }
        
        return list(result_dict.values())
        

    # =========================
    # 判定（核心）
    # =========================

    def should_filter(self, message: DanmakuMessage) -> bool:
        """
        判断一条弹幕是否应被黑名单过滤
        """

        # ---------- 用户黑名单 ----------
        if message.senderId and message.senderId in self._forbidden_users:
            logger.info("Message blocked by forbidden user: {}", message.senderId)
            return True

        # ---------- 用户昵称黑名单（按照文本匹配） ----------
        if isinstance(message, (SuperChatMessage, GiftMessage)) and message.sender:
            for pattern in self._patterns:
                if pattern.search(message.sender):
                    logger.info(
                        "Message blocked by forbidden sender name: {}, triggered by pattern: {}",
                        message.sender,
                        pattern.pattern,
                    )
                    # 替换敏感词
                    message.sender = pattern.sub(
                        lambda m: "*" * len(m.group(0)), message.sender
                    )
                    # return True  # 你可以选择是否过滤整条消息

        # ---------- 文本黑名单 ----------
        if isinstance(message, (PlainDanmakuMessage, SuperChatMessage)):
            text = message.text
        else:
            return False

        if not text:
            return False

        for pattern in self._patterns:
            if pattern.search(text):
                if isinstance(message, SuperChatMessage):
                    # 替换敏感词
                    logger.info(
                        "Message blocked by forbidden sender name: {}, triggered by pattern: {}",
                        message.sender,
                        pattern.pattern,
                    )
                    message.text = pattern.sub(
                        lambda m: "*" * len(m.group(0)), message.text
                    )
                else:
                    logger.info(
                        "Message blocked by blacklist pattern: {}...", text[:20]
                    )
                    return True

        return False

    def close(self) -> None:
        """关闭黑名单服务，释放资源"""
        if self.watchdog:
            self.watchdog.stop()

            # 👇 关键：给 join 一个 timeout
            self.watchdog.join(timeout=1.0)

            if self.watchdog.is_alive():
                logger.warning("Blacklist watchdog did not stop in time")

            self.watchdog = None
            logger.info("Blacklist watchdog stopped")

    # =========================
    # 内部工具
    # =========================

    @staticmethod
    def _load_lines(path: Path) -> list[str]:
        if not path.exists():
            logger.warning("Blacklist file {} not found", path)
            return []

        result: list[str] = []
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        result.append(line)
        except Exception as exc:  # pragma: no cover
            logger.error("Failed to load blacklist {}: {}", path, exc)

        return result


class DanmakuFilter:
    """弹幕过滤器

    功能：
    - 黑名单（正则）
    - 短时间重复弹幕过滤
    """

    def __init__(
        self, blacklist: BlacklistService | None = None, dedup_window: int = 5
    ):
        self.dedup_window = dedup_window  # 去重时间窗口（秒）

        # 记录最近弹幕：
        # group -> deque[(text, timestamp, should_filter)]
        self.recent_messages: dict[str, deque] = defaultdict(deque)

        self.blacklist: BlacklistService | None = blacklist
    
    def append_pattern(self, text: str, hard: bool = False) -> str:
        """动态追加一个黑名单正则模式"""
        if self.blacklist:
            return self.blacklist.append_pattern(text, hard=hard)
        else:
            logger.warning("Cannot append blacklist pattern because blacklist service is not set up")
            return "Cannot append blacklist pattern because blacklist service is not set up"
    
    def remove_pattern(self, text: str, hard: bool = False) -> str:
        """动态移除一个黑名单正则模式"""
        if self.blacklist:
            return self.blacklist.remove_pattern(text, hard=hard)
        else:
            logger.warning("Cannot remove blacklist pattern because blacklist service is not set up")
            return "Cannot remove blacklist pattern because blacklist service is not set up"
    
    def ban_user(self, user_id: str, hard: bool = False) -> str:
        """动态禁止一个用户 ID"""
        if self.blacklist:
            return self.blacklist.ban_user(user_id, hard=hard)
        else:
            logger.warning("Cannot ban user ID because blacklist service is not set up")
            return "Cannot ban user ID because blacklist service is not set up"
    
    def unban_user(self, user_id: str, hard: bool = False) -> str:
        """动态解除禁止一个用户 ID"""
        if self.blacklist:
            return self.blacklist.unban_user(user_id, hard=hard)
        else:
            logger.warning("Cannot unban user ID because blacklist service is not set up")
            return "Cannot unban user ID because blacklist service is not set up"
    
    def list_patterns(self) -> list[dict[str, Any]]:
        """列出当前的黑名单正则模式"""
        if self.blacklist:
            return self.blacklist.list_patterns()
        else:
            logger.warning("Cannot list blacklist patterns because blacklist service is not set up")
            return []
    
    def list_forbidden_users(self) -> list[dict[str, Any]]:
        """列出当前的禁止用户 ID"""
        if self.blacklist:
            return self.blacklist.list_forbidden_users()
        else:
            logger.warning("Cannot list forbidden users because blacklist service is not set up")
            return []

    def should_filter(self, group: str, message: DanmakuMessage) -> bool:
        """判断一条弹幕是否应该被过滤"""

        current_time = time.time()

        # ---------- 黑名单过滤 ----------
        if self.blacklist and self.blacklist.should_filter(message):
            return True

        # ---------- 文字去重过滤 ----------
        if isinstance(message, PlainDanmakuMessage):
            text = message.text

            # dedup_window <= 0 表示不启用去重
            if self.dedup_window > 0:
                recent = self.recent_messages[group]

                # 清理超过时间窗口的历史记录
                while recent and current_time - recent[0][1] > self.dedup_window:
                    recent.popleft()

                # 检查是否出现过完全相同的弹幕
                for recent_text, _ in recent:
                    if recent_text == text:
                        logger.info(f"重复消息被过滤: {text[:20]}...")
                        return True

                # 记录当前弹幕
                recent.append((text, current_time))

        return False

    def close(self) -> None:
        """关闭过滤器，释放资源"""
        if self.blacklist:
            self.blacklist.close()
            self.blacklist = None
            logger.info("DanmakuFilter closed")


# =========================
# WebSocket 连接管理器
# =========================


class ConnectionManager:
    """WebSocket 连接管理器

    管理两类连接：
    - 客户端（观众）
    - 上游（弹幕来源）
    """

    def __init__(
        self,
        danmaku_filter: DanmakuFilter | None = None,
        room_settings_service: RoomSettingsService | None = None,
        emote_resolver: EmoteResolver | None = None,
    ):
        # 客户端连接：
        # group -> set[WebSocket]
        self.client_connections: dict[str, set[WebSocket]] = defaultdict(set)

        # 上游连接（不分 group）
        self.upstream_connections: set[WebSocket] = set()

        self.danmaku_filter = danmaku_filter
        self.room_settings_service = room_settings_service
        self.emote_resolver = emote_resolver

    # ---------- 连接管理 ----------

    async def connect_client(self, websocket: WebSocket, group: str):
        """客户端连接到某个弹幕分组"""
        await websocket.accept()
        self.client_connections[group].add(websocket)
        await self.send_room_settings(websocket, group)
        logger.info(f"客户端连接到群组 {group}")

    async def connect_upstream(self, websocket: WebSocket):
        """上游弹幕源连接"""
        await websocket.accept()
        self.upstream_connections.add(websocket)
        logger.info("上游连接成功")

    def disconnect_client(self, websocket: WebSocket, group: str):
        """断开客户端连接"""
        self.client_connections[group].discard(websocket)
        if not self.client_connections[group]:
            del self.client_connections[group]
        logger.info(f"客户端从群组 {group} 断开")

    def disconnect_upstream(self, websocket: WebSocket):
        """断开上游连接"""
        self.upstream_connections.discard(websocket)
        logger.info("上游连接断开")

    async def disconnect_all(self):
        """断开所有 WebSocket 连接（用于优雅关闭）"""

        # 关闭所有客户端
        for group, websockets in list(self.client_connections.items()):
            for ws in list(websockets):
                try:
                    await ws.close()
                except Exception:
                    pass
            self.client_connections[group].clear()

        # 关闭所有上游
        for ws in list(self.upstream_connections):
            try:
                await ws.close()
            except Exception:
                pass
        self.upstream_connections.clear()

        # 关闭过滤器
        if self.danmaku_filter:
            self.danmaku_filter.close()

    # ---------- 广播逻辑 ----------

    async def broadcast_to_group(self, group: str, message: DanmakuMessage):
        """向指定群组广播弹幕"""

        if group not in self.client_connections:
            return

        # 过滤检查
        if self.danmaku_filter and self.danmaku_filter.should_filter(group, message):
            return

        # 外部表情过滤：到达此处已是 EmoteMessage 的来自外部源
        if (
            isinstance(message, EmoteMessage)
            and self.room_settings_service is not None
            and not self.room_settings_service.get(group).enable_external_emoji
        ):
            return

        # 内置表情解析：将 [emote_name] 文本转为 EmoteMessage
        if (
            isinstance(message, PlainDanmakuMessage)
            and self.emote_resolver is not None
            and self.room_settings_service is not None
            and self.room_settings_service.get(group).enable_internal_emoji
        ):
            emote_urls = self.emote_resolver.resolve(message.text)
            if emote_urls is not None:
                message = MultiEmoteMessage(
                    emote_urls=emote_urls,
                    senderId=message.senderId,
                    sender=message.sender,
                    is_special=message.is_special,
                )

        # 特殊弹幕追加标识
        if message.is_special and isinstance(message, PlainDanmakuMessage):
            message.text += "👑"

        message_json = message.model_dump_json()
        disconnected = []

        # 向所有客户端发送
        for websocket in self.client_connections[group]:
            try:
                await websocket.send_text(message_json)
            except Exception:
                disconnected.append(websocket)

        # 清理失效连接
        for ws in disconnected:
            self.disconnect_client(ws, group)

    def _build_settings_payload(self, group: str) -> str:
        settings = (
            self.room_settings_service.get(group)
            if self.room_settings_service
            else RoomSettings()
        )
        return json.dumps({"type": "settings", "settings": settings.model_dump()})

    async def send_room_settings(self, websocket: WebSocket, group: str):
        await websocket.send_text(self._build_settings_payload(group))

    async def broadcast_room_settings(self, group: str):
        if group not in self.client_connections:
            return

        payload = self._build_settings_payload(group)
        disconnected = []
        for websocket in self.client_connections[group]:
            try:
                await websocket.send_text(payload)
            except Exception:
                disconnected.append(websocket)

        for ws in disconnected:
            self.disconnect_client(ws, group)

    async def broadcast_control_message(self, group: str, action: str):
        """向指定群组广播控制指令（例如清空前端覆盖层）"""
        if group not in self.client_connections:
            return

        payload = json.dumps({"type": "control", "action": action})
        disconnected = []
        for websocket in self.client_connections[group]:
            try:
                await websocket.send_text(payload)
            except Exception:
                disconnected.append(websocket)

        for ws in disconnected:
            self.disconnect_client(ws, group)


class DedupQueue:
    def __init__(self, dedup_window: float, blacklist_window: float = 20):
        self.filter_dedup_window = dedup_window
        self.filter_queue: deque[tuple[tuple[str | None, str], float, bool]] = deque()
        self.filter_seen: set[tuple[str | None, str]] = set()

        self.blacklist_dedup_window = blacklist_window
        self.blacklist_queue: deque[tuple[tuple[str | None, str], float, bool]] = (
            deque()
        )
        self.blacklist_seen: dict[tuple[str | None, str], bool] = dict()

    def _message_key(self, message: "DanmakuMessage") -> tuple:
        """根据弹幕类型动态生成去重/黑名单 key"""
        if isinstance(message, SuperChatMessage) and message.sender:
            return (message.sender, message.text)
        if isinstance(message, GiftMessage) and message.sender:
            return (message.sender, message.gift_name)
        if isinstance(message, PlainDanmakuMessage):
            return (None, message.text)
        if isinstance(message, EmoteMessage):
            return (message.sender, message.emote_url)
        return (None, "")

    def _clean_queue(self):
        now = time.time()
        while (
            self.filter_queue
            and now - self.filter_queue[0][1] > self.filter_dedup_window
        ):
            key, ts, should_filter = self.filter_queue.popleft()
            self.filter_seen.remove(key)
            self.blacklist_queue.append((key, ts, should_filter))
            self.blacklist_seen[key] = should_filter

        while (
            self.blacklist_queue
            and now - self.blacklist_queue[0][1] > self.blacklist_dedup_window
        ):
            key, _, _ = self.blacklist_queue.popleft()
            self.blacklist_seen.pop(key, None)

    def add(
        self, message: DanmakuMessage, blacklist: Optional["BlacklistService"] = None
    ) -> bool:
        self._clean_queue()
        key = self._message_key(message)

        # 短期去重
        if key in self.filter_seen:
            return True

        # 黑名单缓存
        if key in self.blacklist_seen:
            return self.blacklist_seen[key]

        # 黑名单检测
        should_filter = False
        if blacklist and blacklist.should_filter(message):
            should_filter = True

        self.filter_queue.append((key, time.time(), should_filter))
        self.filter_seen.add(key)

        return should_filter
