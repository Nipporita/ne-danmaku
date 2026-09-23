"""弹幕服务路由定义
负责：
- 上游（管理端 / 控制端）WebSocket 接入
- 前端客户端 WebSocket 接入
- 弹幕转发与房间设置管理
"""

import json
from fastapi import WebSocket, WebSocketDisconnect, Query, APIRouter, HTTPException, Request
from hmac import compare_digest
from loguru import logger
from pydantic import BaseModel

from ..config import DanmakuConfig
from .models import ConnectionManager, DanmakuPacket, RoomSettings
from .danmaku_class.danmaku_message import (
    PlainDanmakuMessage,
    SuperChatMessage,
    GiftMessage,
)
from .danmaku_class.danmaku_builder import parse_sc, parse_gift, parse_command


def create_router(config: DanmakuConfig) -> APIRouter:
    """创建弹幕相关的 FastAPI 路由
    
    Args:
        config: 弹幕系统配置（包含上游 token 等）
        
    Returns:
        APIRouter: 已注册弹幕相关接口的路由器
    """
    router = APIRouter()
    
    # ⚠️ 注意：
    # 这里并不直接创建 ConnectionManager
    # 实际的 connection_manager 会在 FastAPI 启动时
    # 由 create_app() 放入 app.state.danmaku_manager
    
    @router.get("/")
    async def root():
        """弹幕服务健康检查接口"""
        return {"message": "弹幕服务运行中", "version": "0.1.0"}

    @router.get("/config")
    async def public_config(request: Request):
        """无需鉴权的公开配置（供前端读取）"""
        return {
            "max_message_length": config.max_message_length,
            "config_version": getattr(request.app.state, "config_reload_counter", 0),
        }

    def validate_admin_token(token: str | None):
        if not token:
            raise HTTPException(status_code=401, detail="Missing admin token")
        if not config.upstream or not compare_digest(token.strip(), config.upstream.token):
            raise HTTPException(status_code=403, detail="Invalid admin token")

    def _get_blacklist(connection_manager: "ConnectionManager"):
        """Get the BlacklistService from the connection manager, or raise 503."""
        if connection_manager.danmaku_filter is None:
            raise HTTPException(status_code=503, detail="Danmaku filter not available")
        if connection_manager.danmaku_filter.blacklist is None:
            raise HTTPException(status_code=503, detail="Blacklist service not available")
        return connection_manager.danmaku_filter.blacklist

    @router.get("/balance")
    async def query_balance_by_name(request: Request, username: str = Query(..., min_length=1)):
        """按用户名查询燕元和燕火余额（无需鉴权）"""
        room_cash_system = getattr(request.app.state, "room_cash_system", None)
        if room_cash_system is None:
            raise HTTPException(status_code=503, detail="Cash system not available")
        results = room_cash_system.get_balance_by_name(username)
        return {"username": username, "balances": results}

    @router.get("/balance_by_id")
    async def query_balance_by_id(request: Request, user_id: str = Query(..., min_length=1)):
        """按用户 ID 查询燕元和燕火余额（无需鉴权）"""
        room_cash_system = getattr(request.app.state, "room_cash_system", None)
        if room_cash_system is None:
            raise HTTPException(status_code=503, detail="Cash system not available")
        results = room_cash_system.get_balance_by_user_id(user_id)
        return {"user_id": user_id, "balances": results}

    @router.get("/admin/rooms/{group}/users")
    async def list_room_users(request: Request, group: str, token: str = Query(None)):
        """列出房间内所有用户（需要鉴权）"""
        validate_admin_token(token)
        room_cash_system = getattr(request.app.state, "room_cash_system", None)
        if room_cash_system is None:
            raise HTTPException(status_code=503, detail="Cash system not available")
        users = room_cash_system.list_room_users(group)
        return {"room_id": group, "users": users}

    class ChargeRequest(BaseModel):
        currency: str  # "huo" or "yuan"
        amount: float

    @router.post("/admin/rooms/{group}/charge/{user_id}")
    async def charge_user(request: Request, group: str, user_id: str, body: ChargeRequest, token: str = Query(None)):
        """为指定用户充值燕火/燕元（需要鉴权）"""
        validate_admin_token(token)
        room_cash_system = getattr(request.app.state, "room_cash_system", None)
        if room_cash_system is None:
            raise HTTPException(status_code=503, detail="Cash system not available")
        if body.currency == "huo":
            result = room_cash_system.charge_huo(group, user_id, body.amount)
        elif body.currency == "yuan":
            result = room_cash_system.charge_yuan(group, user_id, body.amount)
        else:
            raise HTTPException(status_code=400, detail="currency must be 'huo' or 'yuan'")
        if result is None:
            raise HTTPException(status_code=404, detail="User not found in this room")
        return result

    @router.post("/admin/rooms/{group}/charge_all")
    async def charge_all_users(request: Request, group: str, body: ChargeRequest, token: str = Query(None)):
        """为房间所有用户充值燕火/燕元（需要鉴权）"""
        validate_admin_token(token)
        room_cash_system = getattr(request.app.state, "room_cash_system", None)
        if room_cash_system is None:
            raise HTTPException(status_code=503, detail="Cash system not available")
        if body.currency == "huo":
            count = room_cash_system.charge_all_huo(group, body.amount)
        elif body.currency == "yuan":
            count = room_cash_system.charge_all_yuan(group, body.amount)
        else:
            raise HTTPException(status_code=400, detail="currency must be 'huo' or 'yuan'")
        return {"room_id": group, "currency": body.currency, "amount": body.amount, "affected_users": count}

    @router.get("/admin/rooms/{group}/settings")
    async def get_room_settings(request: Request, group: str, token: str = Query(None)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        if connection_manager.room_settings_service is None:
            raise HTTPException(status_code=503, detail="Room settings service not available")
        return connection_manager.room_settings_service.get(group).model_dump()

    @router.put("/admin/rooms/{group}/settings")
    async def update_room_settings(
        request: Request,
        group: str,
        settings: RoomSettings,
        token: str = Query(None),
    ):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        if connection_manager.room_settings_service is None:
            raise HTTPException(status_code=503, detail="Room settings service not available")
        updated = connection_manager.room_settings_service.update(group, settings)
        await connection_manager.broadcast_room_settings(group)
        return updated.model_dump()

    @router.post("/admin/rooms/{group}/clear")
    async def clear_room_overlays(request: Request, group: str, token: str = Query(None)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        await connection_manager.broadcast_control_message(group, "clear_all")
        return {"ok": True, "group": group, "action": "clear_all"}
    
    # ── 运行时配置管理 ──────────────────────────

    @router.get("/admin/config")
    async def get_runtime_config(request: Request, token: str = Query(None)):
        """查看当前运行中的配置（脱敏，排除 upstream.token 等敏感字段）"""
        validate_admin_token(token)
        dc = request.app.state.config.danmaku
        data = dc.model_dump(exclude_none=True)
        # 脱敏
        if "upstream" in data and data["upstream"] is not None:
            data["upstream"]["token"] = "***"
        if "satori" in data and data["satori"] is not None and "token" in data["satori"]:
            data["satori"]["token"] = "***"
        if "bilibili" in data and data["bilibili"] is not None and "sess_data" in data["bilibili"]:
            data["bilibili"]["sess_data"] = "***"
        if "cash" in data and data["cash"] is not None and "secret_key" in data["cash"]:
            data["cash"]["secret_key"] = "***"
        data["config_version"] = getattr(request.app.state, "config_reload_counter", 0)
        return data

    @router.put("/admin/config")
    async def update_runtime_config(
        request: Request,
        token: str = Query(None),
        persist: bool = Query(False),
    ):
        """运行时更新可热加载的配置字段。

        请求体为 JSON 对象，key 为字段的 dotted path（如 ``"max_message_length"``，
        ``"superchat"``，``"cash.initial_huo"``）。只接受可热加载字段。
        设置 ``?persist=true`` 可同时写回 config.json。
        """
        validate_admin_token(token)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON")

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")

        reloader = getattr(request.app.state, "config_reloader", None)
        if reloader is None:
            raise HTTPException(status_code=503, detail="Config reloader not available")

        result = reloader.apply_updates(body, persist=persist)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return result

    @router.post("/admin/rooms/append_pattern")
    async def append_blacklist_pattern(request: Request, pattern: str = Query(...), token: str = Query(None), hard: bool = Query(False)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        bl = _get_blacklist(connection_manager)
        msg = bl.append_pattern(pattern, hard)
        return {"ok": True, "pattern": pattern, "hard": hard, "message": msg}

    @router.post("/admin/rooms/remove_pattern")
    async def remove_blacklist_pattern(request: Request, pattern: str = Query(...), token: str = Query(None), hard: bool = Query(False)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        bl = _get_blacklist(connection_manager)
        msg = bl.remove_pattern(pattern, hard)
        return {"ok": True, "pattern": pattern, "hard": hard, "message": msg}

    @router.post("/admin/rooms/ban_user")
    async def ban_user(request: Request, user_id: str = Query(...), token: str = Query(None)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        bl = _get_blacklist(connection_manager)
        msg = bl.ban_user(user_id)
        return {"ok": True, "user_id": user_id, "message": msg}

    @router.post("/admin/rooms/unban_user")
    async def unban_user(request: Request, user_id: str = Query(...), token: str = Query(None)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        bl = _get_blacklist(connection_manager)
        msg = bl.unban_user(user_id)
        return {"ok": True, "user_id": user_id, "message": msg}

    @router.get("/admin/rooms/list_patterns")
    async def list_blacklist_patterns(request: Request, token: str = Query(None)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        bl = _get_blacklist(connection_manager)
        patterns = bl.list_patterns()
        return {"ok": True, "patterns": patterns}

    @router.get("/admin/rooms/list_banned_users")
    async def list_banned_users(request: Request, token: str = Query(None)):
        validate_admin_token(token)
        connection_manager: ConnectionManager = request.app.state.danmaku_manager
        bl = _get_blacklist(connection_manager)
        banned_users = bl.list_forbidden_users()
        return {"ok": True, "banned_users": banned_users}

    @router.websocket("/upstream")
    async def upstream_websocket(websocket: WebSocket, token: str = Query(None)):
        """上游弹幕 WebSocket
        
        用途：
        - 接收管理端 / 控制端发送的弹幕
        - 需要通过 token 鉴权
        """
        
        # 从 FastAPI 应用状态中获取连接管理器
        app = websocket.scope.get("app")
        if app is None:
            await websocket.close(code=1011, reason="Server configuration error")
            return
        connection_manager: ConnectionManager = app.state.danmaku_manager

        # 未提供 token，直接拒绝连接
        if not token:
            await websocket.close(code=1008, reason="Missing authorization token")
            return

        # 解析 token（目前只做简单 trim，兼容直接 token 形式）
        final_token = token.strip()

        # 校验 token（使用 compare_digest 防止时序攻击）
        if not config.upstream or not compare_digest(final_token, config.upstream.token):
            await websocket.close(code=1008, reason="Invalid token")
            return

        # 接受上游连接
        await connection_manager.connect_upstream(websocket)

        try:
            while True:
                # 接收上游发送的原始文本数据
                data = await websocket.receive_text()

                try:
                    raw = json.loads(data)
                    group = raw.get("group", "")
                    danmaku_raw = raw.get("danmaku", {})

                    if "type" in danmaku_raw:
                        # 已有 type 字段，按原有逻辑解析
                        packet = DanmakuPacket.model_validate(raw)
                        message = packet.danmaku
                    else:
                        # 无 type 字段：来自前端管理面板的简单格式
                        # 解析 /sc、/gift、颜色/置顶等命令
                        text = danmaku_raw.get("text", "").strip()
                        sender = danmaku_raw.get("sender")
                        sender_id = danmaku_raw.get("senderId")

                        sc_info = parse_sc(text)
                        gift_info = parse_gift(text)

                        if sc_info is not None:
                            message = SuperChatMessage(
                                sender=sender,
                                senderId=sender_id,
                                **sc_info,
                            )
                        elif gift_info is not None:
                            message = GiftMessage(
                                sender=sender,
                                senderId=sender_id,
                                **gift_info,
                            )
                        else:
                            cmd = parse_command(text)
                            message = PlainDanmakuMessage(
                                sender=sender,
                                senderId=sender_id,
                                **(cmd if cmd is not None else {"text": text}),
                            )

                    # 上游发送的弹幕统一标记为特殊弹幕
                    message.is_special = True

                    # 服务端字数上限校验
                    if isinstance(message, PlainDanmakuMessage) and len(message.text) > config.max_message_length:
                        await websocket.send_text(
                            json.dumps({"error": f"弹幕长度超过上限（{config.max_message_length} 字符）"})
                        )
                        continue
                    if isinstance(message, SuperChatMessage) and len(message.text) > config.max_message_length:
                        await websocket.send_text(
                            json.dumps({"error": f"SC 文本长度超过上限（{config.max_message_length} 字符）"})
                        )
                        continue

                    # 广播弹幕到指定 group
                    await connection_manager.broadcast_to_group(
                        group, message
                    )

                except Exception as e:
                    # 数据格式错误或处理异常
                    logger.error("处理上游消息错误: {}", e)
                    await websocket.send_text(
                        json.dumps({"error": "Invalid message format"})
                    )

        except WebSocketDisconnect:
            # 上游连接断开时清理
            connection_manager.disconnect_upstream(websocket)

    @router.websocket("/danmaku/{group}")
    async def client_websocket(websocket: WebSocket, group: str):
        """客户端弹幕 WebSocket
        
        特点：
        - 前端页面使用
        - 只接收服务器推送的弹幕
        - 客户端发送的任何内容都会被忽略
        """
        # 从 FastAPI 应用状态中获取连接管理器
        app = websocket.scope.get("app")
        if app is None:
            await websocket.close(code=1011, reason="Server configuration error")
            return
        connection_manager: ConnectionManager = app.state.danmaku_manager

        # 客户端加入指定弹幕组
        await connection_manager.connect_client(websocket, group)

        try:
            # 保持连接存活
            # 客户端发送的任何消息都会被直接丢弃
            while True:
                await websocket.receive_text()

        except WebSocketDisconnect:
            # 客户端断开时清理连接
            connection_manager.disconnect_client(websocket, group)
    
    return router
