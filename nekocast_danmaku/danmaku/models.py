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
    BLOCK_REASON_DUPLICATE,
    BLOCK_REASON_USER,
    BLOCK_REASON_WORD,
    DanmakuMessage,
    EmoteMessage,
    MultiEmoteMessage,
    GiftMessage,
    PlainDanmakuMessage,
    SuperChatMessage,
)

from ..emotes.resolver import EmoteResolver
from .inline_elements import segments as inline_segments, to_plain_text


def annotate_inline_elements(message: Any) -> None:
    """把文本里的行内元素（``<face id="339" type="2" platform="llonebot"/>`` 之类）解析出来。

    上游会把表情这类"不是纯文字"的段塞进消息文本里。显示层要能**行内渲染**它
    （见 ``message.inline``），而游戏那种只认纯文本的消费者需要一个已经替换干净的
    版本（``message.text_plain``，元素一律换成 █）。

    ``platform`` 之类的附加属性不做处理（只在 attrs / raw 里）；"整条只有元素"
    的消息也不特殊对待 —— 对游戏来说它的纯文本就是 ``█``，照样算数。

    没有行内元素时不动任何字段（保持帧尽量小）。
    """
    raw = getattr(message, "text", "") or ""
    if "<" not in raw:
        return
    segs = inline_segments(raw)
    if not segs or all(seg.get("kind") == "text" for seg in segs):
        return
    message.inline = segs
    message.text_plain = to_plain_text(raw)
    logger.info(
        "行内元素 {} 段（纯文本：{}）",
        sum(1 for seg in segs if seg.get("kind") != "text"),
        message.text_plain,
    )


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

# 第三方词表（blacklist_extra_files）里，短于这个长度的词直接丢弃。
# 单字在弹幕里太常见（"草""操"这类），当过滤词会把大量正常弹幕也标成 blocked。
MIN_LITERAL_PATTERN_LEN = 2


class BlacklistService:
    """
    黑名单服务（只负责“是否应该被过滤”这一件事）

    功能：
    - 文本正则黑名单
    - 发送者 ID 黑名单
    """

    def __init__(self, extra_pattern_files: list[Path] | None = None):
        import threading

        self._lock = threading.Lock()

        # 已编译的正则
        self._patterns: list[regex.Pattern] = []
        self._pattern_strings: set[str] = set()  # 用于快速检查是否已存在某个模式字符串

        self._pattern_in_file: list[str] = []  # 维护一个原始字符串列表，保持与文件一致的顺序（用于持久化）

        # 每条规则来自哪个文件（排查误伤时很有用：能直接指出"是哪个词库哪一行"）
        self._pattern_sources: dict[str, str] = {}

        # 额外的「词表」文件（第三方开源词库，一般以 submodule 引入，见 config 的
        # blacklist_extra_files）。它们按**字面量**匹配，而且**不进** _pattern_in_file：
        # 管理端的增删改只应作用于主黑名单文件，不该去改写别人仓库里的词表。
        self._extra_pattern_files: list[Path] = [Path(p) for p in (extra_pattern_files or [])]

        # 禁止用户 ID
        self._forbidden_users: set[str] = set()
        self._forbidden_users_in_file: list[str] = []  # 维护一个原始用户 ID 列表，保持与文件一致的顺序（用于持久化）

        self.watchdog: Any = None  # 文件监视器（外部设置）
        
        self.handler: Any = None  # Watchdog 事件处理器（外部设置）

    @property
    def extra_pattern_files(self) -> list[Path]:
        return list(self._extra_pattern_files)

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
        self._pattern_sources = {pat.pattern: str(path) for pat in compiled}
        logger.info("Loaded {} blacklist regex patterns", len(compiled))

    def load_literal_patterns(self, path: Path) -> int:
        """加载一份「词表」：每行一个词，按**字面量**匹配，不做正则解析。

        第三方开源词库（submodule 进来的那种）都是普通词表，里面的 `*`、`(`
        只是普通字符；按正则解析会变成通配符，误伤会大得离谱。
        长度 < MIN_LITERAL_PATTERN_LEN 的词也丢掉：单字在弹幕里太常见
        （"草""操"这种），当过滤词会把大半正常弹幕也标成 blocked。
        """
        words = self._load_lines(path)
        added = skipped_short = duplicated = 0
        for word in words:
            if len(word) < MIN_LITERAL_PATTERN_LEN:
                skipped_short += 1
                continue
            try:
                compiled = regex.compile(regex.escape(word), regex.IGNORECASE)
            except regex.error as exc:
                logger.error("Invalid literal pattern '{}': {}", word, exc)
                continue
            if compiled.pattern in self._pattern_strings:
                duplicated += 1
                continue
            self._patterns.append(compiled)
            self._pattern_strings.add(compiled.pattern)
            self._pattern_sources[compiled.pattern] = str(path)
            added += 1
        logger.info(
            "Loaded {} literal patterns from {} (skipped {} too short, {} duplicated)",
            added, path.name, skipped_short, duplicated,
        )
        return added

    def load_users(self, path: Path) -> None:
        self._forbidden_users = set(self._load_lines(path))
        self._forbidden_users_in_file = list(self._forbidden_users)  # 保持原始用户 ID 列表
        logger.info("Loaded {} forbidden users", len(self._forbidden_users))

    def reload(self, pattern_path: Path, user_path: Path) -> None:
        self.load_patterns(pattern_path)
        for extra in self._extra_pattern_files:
            if Path(extra).exists():
                self.load_literal_patterns(Path(extra))
            else:
                logger.warning("Extra blacklist file not found, skipped: {}", extra)
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

    def check_message(self, message: DanmakuMessage) -> None:
        """
        检查一条弹幕是否应被标记为 blocked。

        注意：此方法只负责标记 ``message.blocked``，不再丢弃消息。
        所有消息都会广播到前端，由前端根据 ``blocked`` 字段决定是否展示。
        """

        # ---------- 行内元素（<face .../> 之类） ----------
        # 放在最前面：所有消息类型都要过这一步（后面按类型 early return 了）
        annotate_inline_elements(message)

        # 线程安全：快照引用后释放锁，避免在 regex 匹配期间持有锁
        with self._lock:
            forbidden_users = self._forbidden_users.copy()
            patterns = list(self._patterns)

        # ---------- 用户黑名单 ----------
        if message.senderId and message.senderId in forbidden_users:
            logger.info("Message blocked by forbidden user: {}", message.senderId)
            message.mark_blocked(BLOCK_REASON_USER)

        # ---------- 用户昵称黑名单（按照文本匹配） ----------
        if isinstance(message, (SuperChatMessage, GiftMessage)) and message.sender:
            for pattern in patterns:
                if pattern.search(message.sender):
                    logger.info(
                        "Sender name censored by pattern: {}, triggered by: {}",
                        message.sender,
                        pattern.pattern,
                    )
                    # 替换敏感词
                    message.sender = pattern.sub(
                        lambda m: "*" * len(m.group(0)), message.sender
                    )

        # ---------- 文本黑名单 ----------
        if isinstance(message, (PlainDanmakuMessage, SuperChatMessage)):
            text = message.text
        else:
            return

        if not text:
            return

        for pattern in patterns:
            if pattern.search(text):
                if isinstance(message, SuperChatMessage):
                    # SC 打码（展示在屏幕上，需要脱敏）
                    message.text = pattern.sub(
                        lambda m: "*" * len(m.group(0)), message.text
                    )
                    logger.info(
                        "SC text censored by pattern: {}, sender={}",
                        pattern.pattern,
                        message.sender,
                    )
                else:
                    # 普通弹幕：保留原文，只标记 blocked（游戏指令等需要完整文本）
                    logger.info(
                        "Message blocked by pattern: {}, text={}...",
                        pattern.pattern,
                        text[:20],
                    )
                    message.mark_blocked(BLOCK_REASON_WORD)

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
        # group -> deque[((发送者, 文本), timestamp)]
        self.recent_messages: dict[str, deque] = defaultdict(deque)

        self.blacklist: BlacklistService | None = blacklist
    

    def check_message(self, group: str, message: DanmakuMessage) -> None:
        """检查弹幕并标记 blocked 标志，不再丢弃消息"""

        current_time = time.time()

        # ---------- 黑名单过滤 ----------
        if self.blacklist:
            self.blacklist.check_message(message)

        # ---------- 文字去重过滤 ----------
        if isinstance(message, PlainDanmakuMessage):
            text = message.text

            # dedup_window <= 0 表示不启用去重
            if self.dedup_window > 0:
                recent = self.recent_messages[group]

                # 清理超过时间窗口的历史记录
                while recent and current_time - recent[0][1] > self.dedup_window:
                    recent.popleft()

                # 去重键 = (发送者, 文本)。
                # 只拦"同一个人短时间内的重复弹幕"；不同的人发同一句话（报名刷「1」、
                # 打分刷同一个数字）必须都能放行，所以 key 里必须带上发送者。
                sender_key = message.senderId or message.sender or ""
                key = (sender_key, text)

                # 检查是否出现过完全相同的弹幕
                for recent_key, _ in recent:
                    if recent_key == key:
                        logger.info("重复消息被标记: {} / {}...", sender_key, text[:20])
                        message.mark_blocked(BLOCK_REASON_DUPLICATE)
                        break
                else:
                    # 不是重复才记录（重复消息不占去重槽位）
                    recent.append((key, current_time))

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
        max_message_length: int = 50,
    ):
        # 客户端连接：
        # group -> set[WebSocket]
        self.client_connections: dict[str, set[WebSocket]] = defaultdict(set)

        # 上游连接（不分 group）
        self.upstream_connections: set[WebSocket] = set()

        self.danmaku_filter = danmaku_filter
        self.room_settings_service = room_settings_service
        self.emote_resolver = emote_resolver
        self.max_message_length = max_message_length

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

        # 这条消息"到达时"是不是外部图片（群里贴的图），供下面那个房间开关判断。
        # 先记下来：内置表情解析会把 Plain 变成 EmoteMessage，之后就区分不出来了。
        arrived_as_external_emote = isinstance(message, EmoteMessage)

        # 内置表情解析：将 [emote_name] 文本转为 EmoteMessage。
        # ⚠ 必须放在黑名单检查之前：blacklist.txt 第一条就是 ^\[.*\]$，
        #    会把整条 [名字] 先标记成 blocked，解析器根本轮不到。
        #    （表情名是自己配的内容，没必要再走文本黑名单；
        #      用户黑名单对下面的 EmoteMessage 依然生效。）
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
                    blocked=message.blocked,
                )

        # 过滤检查（标记 blocked 标志，不丢弃消息；黑名单 + 同一发送者短时间重复）
        if self.danmaku_filter:
            self.danmaku_filter.check_message(group, message)

        # 外部表情过滤：只针对"群里贴的原图"，受房间开关控制
        if (
            arrived_as_external_emote
            and self.room_settings_service is not None
            and not self.room_settings_service.get(group).enable_external_emoji
        ):
            return

        # 字数上限截断（对外部来源静默截断，上游 WS 已有前置校验）
        # 必须放在表情解析之后：EMOTE_PATTERN 要求 [name] 完整闭合，
        # 先截断会把跨边界的 "[表情名" 截成无法匹配的字面量
        if isinstance(message, PlainDanmakuMessage) and len(message.text) > self.max_message_length:
            logger.info("弹幕过长（{} 字符），已截断", len(message.text))
            message.text = message.text[: self.max_message_length]
        if isinstance(message, SuperChatMessage) and len(message.text) > self.max_message_length:
            logger.info("SC 文本过长（{} 字符），已截断", len(message.text))
            message.text = message.text[: self.max_message_length]

        # 特殊弹幕追加标识
        if message.is_special and isinstance(message, PlainDanmakuMessage):
            message.text += "👑"

        await self._send_and_prune(group, message.model_dump_json())

    async def _send_and_prune(self, group: str, payload: str) -> None:
        """向 group 内所有客户端发送 *payload*，并清理发送失败的连接"""
        if group not in self.client_connections:
            return

        disconnected = []
        for websocket in self.client_connections[group]:
            try:
                await websocket.send_text(payload)
            except Exception:
                disconnected.append(websocket)

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
        await self._send_and_prune(group, self._build_settings_payload(group))

    async def broadcast_control_message(self, group: str, action: str):
        """向指定群组广播控制指令（例如清空前端覆盖层）"""
        await self._send_and_prune(group, json.dumps({"type": "control", "action": action}))

    async def broadcast_frame(self, group: str, payload: dict):
        """向指定群组广播一条「非弹幕」事件帧。

        用于撤回（recall）、贴表情（reaction）这类不携带 text/sender 的事件：
        它们不能走 ``broadcast_to_group`` —— 那条路会做黑名单/去重标记、
        内置表情解析、补 👑 等只对弹幕有意义的处理。
        客户端只需要额外认一个 ``type`` 字段，老客户端会走不到任何分支而忽略。
        """
        await self._send_and_prune(
            group, json.dumps(payload, ensure_ascii=False)
        )

    async def broadcast_config(self, payload: dict):
        """向所有分组的客户端广播配置变更。

        配置是全局的（不区分 group），因此与 ``broadcast_room_settings``
        不同，这里扇出到每一个已连接的分组。
        """
        data = json.dumps(payload)
        # list() 快照：_send_and_prune 可能删掉空 group
        for group in list(self.client_connections):
            await self._send_and_prune(group, data)
