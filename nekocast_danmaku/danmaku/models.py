"""弹幕数据模型"""

import json
import regex
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import WebSocket
from loguru import logger
from pydantic import BaseModel, model_validator


class DanmakuMessage(BaseModel):
    """弹幕消息结构"""

    text: str
    color: str | None = None
    size: int | None = None
    sender: str | None = None
    is_special: bool = False


class DanmakuControl(BaseModel):
    """弹幕控制指令"""

    type: Literal["setOpacity"]
    value: float

    @model_validator(mode="after")
    def clamp_value(self):
        # 将值限制在 0-100 范围
        self.value = max(0.0, min(100.0, self.value))
        return self


class DanmakuPacket(BaseModel):
    """上游弹幕数据包结构"""

    group: str
    danmaku: DanmakuMessage | None = None
    control: DanmakuControl | None = None

    @model_validator(mode="after")
    def ensure_payload(self):
        if not self.danmaku and not self.control:
            raise ValueError("Packet must include danmaku or control payload")
        return self


class DanmakuFilter:
    """弹幕过滤器"""

    def __init__(self, dedup_window: int = 5, blacklists: list[str] | None = None):
        self.dedup_window = dedup_window  # 去重窗口（秒）
        self.recent_messages: dict[str, deque] = defaultdict(
            deque
        )  # group -> [(text, timestamp), ...]
        self.blacklist_patterns: list[regex.Pattern] = []

        # 加载屏蔽词
        for pattern in blacklists or []:
            try:
                self.blacklist_patterns.append(regex.compile(pattern, regex.IGNORECASE))
            except regex.error as e:
                logger.error(f"无效的正则表达式: {pattern}, 错误: {e}")

    def should_filter(self, group: str, message: DanmakuMessage) -> bool:
        """检查消息是否应该被过滤"""
        text = message.text
        current_time = time.time()

        # 检查屏蔽词
        for pattern in self.blacklist_patterns:
            if pattern.search(text):
                logger.info(f"消息被屏蔽词过滤: {text[:20]}...")
                return True

        # 检查去重（如果去重窗口为-1则不去重）
        if self.dedup_window > 0:
            recent = self.recent_messages[group]

            # 清理过期消息
            while recent and current_time - recent[0][1] > self.dedup_window:
                recent.popleft()

            # 检查是否重复
            for recent_text, _ in recent:
                if recent_text == text:
                    logger.info(f"重复消息被过滤: {text[:20]}...")
                    return True

            # 添加到历史记录
            recent.append((text, current_time))

        return False


class ConnectionManager:
    """WebSocket连接管理器"""

    def __init__(self, danmaku_filter: DanmakuFilter | None = None):
        # 客户端连接：group -> set[WebSocket]
        self.client_connections: dict[str, set[WebSocket]] = defaultdict(set)
        # 上游连接
        self.upstream_connections: set[WebSocket] = set()
        self.danmaku_filter = danmaku_filter

    async def connect_client(self, websocket: WebSocket, group: str):
        """连接客户端"""
        await websocket.accept()
        self.client_connections[group].add(websocket)
        logger.info(f"客户端连接到群组 {group}")

    async def connect_upstream(self, websocket: WebSocket):
        """连接上游"""
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
        """断开所有连接"""
        # 关闭所有客户端连接
        for group, websockets in list(self.client_connections.items()):
            for ws in list(websockets):
                try:
                    await ws.close()
                except Exception:
                    pass
            self.client_connections[group].clear()
        
        # 关闭所有上游连接
        for ws in list(self.upstream_connections):
            try:
                await ws.close()
            except Exception:
                pass
        self.upstream_connections.clear()

    async def broadcast_to_group(self, group: str, message: DanmakuMessage):
        """向指定群组广播消息"""
        if group not in self.client_connections:
            return

        if self.danmaku_filter and self.danmaku_filter.should_filter(group, message):
            return
        
        if message.is_special:
            message.text += "👑"

        message_json = message.model_dump_json()
        disconnected = []

        for websocket in self.client_connections[group]:
            try:
                await websocket.send_text(message_json)
            except Exception:
                disconnected.append(websocket)

        # 清理断开的连接
        for ws in disconnected:
            self.disconnect_client(ws, group)

    async def broadcast_control(self, group: str, control: DanmakuControl):
        """向指定群组广播控制指令"""
        if group not in self.client_connections:
            return

        payload = json.dumps({"type": "control", "control": control.model_dump()})
        disconnected = []

        for websocket in self.client_connections[group]:
            try:
                await websocket.send_text(payload)
            except Exception:
                disconnected.append(websocket)

        for ws in disconnected:
            self.disconnect_client(ws, group)
