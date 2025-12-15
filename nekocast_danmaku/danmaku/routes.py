"""弹幕服务路由定义"""

import json
from fastapi import WebSocket, WebSocketDisconnect, Query, APIRouter
from hmac import compare_digest
from loguru import logger

from ..config import DanmakuConfig
from .models import ConnectionManager, DanmakuPacket


def create_router(config: DanmakuConfig) -> APIRouter:
    """创建弹幕路由
    
    Args:
        config: 弹幕配置
        
    Returns:
        APIRouter: 弹幕路由器
    """
    router = APIRouter()
    
    # 这里的 connection_manager 将在应用启动时从 app.state 中获取
    
    @router.get("/")
    async def root():
        """根路径"""
        return {"message": "弹幕服务运行中", "version": "0.1.0"}

    @router.websocket("/upstream")
    async def upstream_websocket(websocket: WebSocket, token: str = Query(None)):
        """上游弹幕接收WebSocket"""
        
        # 从 app.state 获取 connection_manager
        request = websocket.scope.get("app")
        connection_manager: ConnectionManager = request.state.danmaku_manager
        
        if not token:
            await websocket.close(code=1008, reason="Missing authorization token")
            return

        # 解析token（支持Bearer格式或直接token）
        final_token = token.strip()

        if not config.upstream or not compare_digest(final_token, config.upstream.token):
            await websocket.close(code=1008, reason="Invalid token")
            return

        await connection_manager.connect_upstream(websocket)

        try:
            while True:
                # 接收上游数据
                data = await websocket.receive_text()

                try:
                    packet = DanmakuPacket.model_validate_json(data)

                    if packet.control:
                        await connection_manager.broadcast_control(
                            packet.group, packet.control
                        )
                        continue

                    packet.danmaku.is_special = True
                    await connection_manager.broadcast_to_group(
                        packet.group, packet.danmaku
                    )

                except Exception as e:
                    logger.error(f"处理上游消息错误: {e}")
                    await websocket.send_text(
                        json.dumps({"error": f"Invalid message format: {e}"})
                    )

        except WebSocketDisconnect:
            connection_manager.disconnect_upstream(websocket)

    @router.websocket("/danmaku/{group}")
    async def client_websocket(websocket: WebSocket, group: str):
        """客户端弹幕WebSocket - 只接收弹幕，不允许发送"""
        # 从 app.state 获取 connection_manager
        request = websocket.scope.get("app")
        connection_manager: ConnectionManager = request.state.danmaku_manager
        
        await connection_manager.connect_client(websocket, group)

        try:
            # 保持连接活跃，只接收弹幕不处理客户端发送的消息
            while True:
                await websocket.receive_text()  # 忽略客户端发送的任何消息

        except WebSocketDisconnect:
            connection_manager.disconnect_client(websocket, group)
    
    return router
