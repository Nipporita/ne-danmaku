"""Satori 弹幕客户端"""

from satori.client import App, WebsocketsInfo, EventType, Account
from satori.event import MessageEvent
from satori.element import Text
from launart import Launart
from graia.amnesia.builtins.aiohttp import AiohttpClientService
from asyncio import create_task, Task
import regex
from loguru import logger

from ..config import SatoriConfig
from .models import ConnectionManager, DanmakuMessage


satori_task: Task | None = None
launart: Launart | None = None


async def start_satori_client(
    config: SatoriConfig, connection_manager: ConnectionManager
) -> Task:
    """启动 Satori 客户端
    
    Args:
        config: Satori 配置
        connection_manager: 连接管理器
        
    Returns:
        Task: Satori 任务
    """
    global satori_task, launart
    
    if satori_task is not None:
        logger.warning("Satori 客户端已经在运行")
        return satori_task
    
    launart = Launart()
    client = App(
        WebsocketsInfo(
            host=config.host,
            port=config.port,
            path=config.path,
            token=config.token,
        )
    )

    @client.register_on(EventType.MESSAGE_CREATED)
    async def handle_message(account: Account, event: MessageEvent):
        if event.channel.id not in config.group_map:
            logger.warning("收到未配置频道的弹幕，频道 ID: {}", event.channel.id)
            return

        danmaku_channel = config.group_map[event.channel.id]
        username = event.member.nick or event.user.nick or event.user.name or "匿名"

        elements = event.message.message

        logger.debug("收到弹幕，频道: {}, 用户: {}, 内容: {}", danmaku_channel, username, elements)
        if not all(isinstance(i, Text) for i in elements):
            return
        
        text = "".join(i.text for i in elements if isinstance(i, Text)).strip()
        if matches := regex.match(r"^(.+)(#[0-9a-fA-F]{6})$", text):
            text = matches.group(1)
            color = matches.group(2)
        else:
            color = None
        danmaku = DanmakuMessage(
            text=text,
            sender=username,
            color=color,
        )

        await connection_manager.broadcast_to_group(danmaku_channel, danmaku)

    launart.add_component(AiohttpClientService())
    launart.add_component(client)
    satori_task = create_task(launart.launch(), name="satori")
    logger.info("Satori 客户端正在启动")
    
    return satori_task


async def stop_satori_client():
    """停止 Satori 客户端"""
    global satori_task, launart
    
    if satori_task is not None and launart is not None:
        launart._on_sys_signal(None, None, main_task=satori_task)
        await satori_task
        satori_task = None
        launart = None
        logger.info("Satori 客户端已停止")
