"""Satori 弹幕客户端"""
# 本模块用于连接 Satori 平台，并将收到的弹幕转发到本地弹幕系统

from satori.client import App, WebsocketsInfo, EventType, Account
# App：Satori 客户端主对象
# WebsocketsInfo：WebSocket 连接信息
# EventType：事件类型枚举（如消息创建）
# Account：账号信息

from satori.event import MessageEvent
# MessageEvent：消息事件对象

from launart import Launart
# Launart：异步组件生命周期管理框架（Satori 官方推荐）

from graia.amnesia.builtins.aiohttp import AiohttpClientService
# Aiohttp 客户端服务，用于 WebSocket / HTTP 底层通信

from asyncio import create_task, Task
from loguru import logger

from ..config import SatoriConfig
# Satori 的配置结构

from .models import ConnectionManager
from .game_events import member_is_admin, recall_frame, reaction_frame
from .event_probe import mark_handled, record_satori_event
from .cash_system import RoomCashSystem, GiftCodeFormatError, GiftCodeRedeemError
from .danmaku_class.danmaku_builder import DanmakuBuilder
from .danmaku_class.danmaku_message import EmoteMessage, GiftMessage, PlainDanmakuMessage, SuperChatMessage
# ConnectionManager：本地弹幕连接管理器
# DanmakuMessage：统一的弹幕消息数据结构

from ..emoji.cache import EmojiCache

from ..emotes.append_emotes import append_emotes
from pathlib import Path

import re
# room_id_pattern = "群名查询余额" 如 "弹幕群查询余额"
ROOM_ID_PATTERN = re.compile(
    r"^\s*(.+?)\s*查询余额\s*$",
    re.IGNORECASE,
)

# =========================
# 游戏侧事件帧（撤回 / 贴表情）与身份辅助
# =========================
# 线格式见 game_events.py；这里只负责把 Satori 的事件对象适配过去。


def _sid(obj) -> str:
    value = getattr(obj, "id", None)
    return "" if value is None else str(value)


def build_recall_frame(event) -> dict:
    """群消息被撤回 → 事件帧"""
    return recall_frame(
        _sid(getattr(event, "message", None)),
        sender_id=_sid(getattr(event, "user", None)),
        operator_id=_sid(getattr(event, "operator", None)),
        channel_id=_sid(getattr(event, "channel", None)),
    )


def build_reaction_frame(event, action="add") -> dict:
    """有人给某条消息贴了（或取消了）表情 → 事件帧"""
    return reaction_frame(
        _sid(getattr(event, "message", None)),
        sender_id=_sid(getattr(event, "user", None)),
        emoji=_sid(getattr(event, "emoji", None)),
        channel_id=_sid(getattr(event, "channel", None)),
        action=action,
    )


async def forward_recall(config, connection_manager, event) -> bool:
    """撤回事件 → 广播给对应房间的大屏（返回是否真的推出去了）"""
    frame = build_recall_frame(event)
    danmaku_channel = config.group_map.get(frame["channel_id"])
    if danmaku_channel is None:
        logger.debug("撤回事件来自未配置频道，忽略: {}", frame)
        return False
    if not frame["message_id"]:
        return False
    logger.info(
        "撤回：频道 {}, 消息 {}, 作者 {}, 操作者 {}",
        danmaku_channel,
        frame["message_id"],
        frame["sender_id"],
        frame["operator_id"],
    )
    await connection_manager.broadcast_frame(danmaku_channel, frame)
    return True


async def forward_reaction(config, connection_manager, event, member_admin=None, action="add") -> bool:
    """贴表情 / 取消表情事件 → 广播给对应房间的大屏（游戏侧用作答题审核）

    ``action="remove"`` 表示主持人把表情取消了 —— 游戏侧理解为「取消审核」。

    ``member_admin`` 是「成员 id → 是否群主/管理员」的缓存（on_ready 时填充）。
    贴上它很有必要：贴表情事件本身不带角色信息，而游戏侧要靠角色认定「谁有资格审核」——
    主持人如果只在控制面板上操作、从没在群里发过言，前端就还没把他绑定成主持人。
    """
    frame = build_reaction_frame(event, action=action)
    danmaku_channel = config.group_map.get(frame["channel_id"])
    if danmaku_channel is None:
        logger.debug("表情事件来自未配置频道，忽略: {}", frame)
        return False
    if not frame["message_id"]:
        return False
    if member_admin is not None:
        frame["is_admin"] = bool(member_admin.get(frame["sender_id"], False))
    logger.info(
        "表情事件：{}，频道 {}, 消息 {}, 用户 {}, 表情 {}, 管理员 {}",
        "取消" if frame["action"] == "remove" else "贴",
        danmaku_channel,
        frame["message_id"],
        frame["sender_id"],
        frame["emoji"],
        frame.get("is_admin"),
    )
    await connection_manager.broadcast_frame(danmaku_channel, frame)
    return True


# =========================
# 全局状态（确保只启动一个客户端）
# =========================

satori_task: Task | None = None
# Satori 客户端运行的 asyncio 任务

launart: Launart | None = None
# Launart 实例，用于管理客户端生命周期

async def start_satori_client(
    config: SatoriConfig,
    connection_manager: ConnectionManager,
    emoji_cache: EmojiCache,
    room_cash_system: RoomCashSystem,
) -> Task:
    """启动 Satori 客户端
    
    Args:
        config: Satori 配置
        connection_manager: 弹幕连接管理器（用于广播弹幕）
        
    Returns:
        Task: Satori 客户端对应的 asyncio 任务
    """
    global satori_task, launart

    # 如果已经启动过客户端，则直接复用
    if satori_task is not None:
        logger.warning("Satori 客户端已经在运行")
        return satori_task

    # 事件探针：告诉它哪些类型是本客户端真正处理的（其余类型出现即"上游推了但没处理"）
    mark_handled(
        str(EventType.MESSAGE_CREATED.value),
        str(EventType.MESSAGE_DELETED.value),
        str(EventType.REACTION_ADDED.value),
        str(EventType.REACTION_REMOVED.value),
        str(EventType.GUILD_MEMBER_ADDED.value),
    )

    # 创建 Launart 生命周期管理器
    launart = Launart()

    # 创建 Satori 客户端实例
    client = App(
        WebsocketsInfo(
            host=config.host,
            port=config.port,
            path=config.path,
            token=config.token,
        )
    )

    # =========================
    # 注册消息事件处理器 （需要大改）
    # =========================
    
    users = set()  # 用于记录已获取过的用户 ID，避免重复日志
    member_admin: dict = {}  # 成员 id -> 是否群主/管理员（贴表情事件不带角色，靠这里的缓存补）

    @client.register_on(EventType.MESSAGE_CREATED)
    async def handle_message(account: Account, event: MessageEvent):
        # 收到一条新消息（弹幕）
        
        # 只能shortcut了
        # path = Path(r"D:\项目\元火\ne-danmaku\assets_danmaku\emotes")
        # await append_emotes(path, event.message.message)
        
        # 可能是查询余额
        m = ROOM_ID_PATTERN.match(event.message.content)
        if m:
            userid = event.user.id
            danmaku_channel = m.group(1).strip()
            if danmaku_channel in config.group_map.values():
                cash_user_id = str(userid) if userid else f"name:{event.user.name}"
                balance = room_cash_system.get_balance(room_id=danmaku_channel, user_id=cash_user_id, user_name=str(event.user.name))
                if balance is None:
                    logger.warning(
                        "查询余额失败，频道: {}, 用户 {}-{} 不在系统中",
                        danmaku_channel,
                        userid,
                        event.user.name,
                    )
                    await account.send_message(
                        event.channel.id,
                        f"你在 {danmaku_channel} 的余额是 0 弹货，0 P点",
                    )
                else:
                    logger.info(
                        "用户 {}-{} 查询余额，频道: {}, 余额: {} 元, {} 火",
                        userid,
                        event.user.name,
                        danmaku_channel,
                        balance[0],
                        balance[1],
                    )
                    await account.send_message(
                        event.channel.id,
                        f"你在 {danmaku_channel} 的余额是 {balance[0]} 弹货，{balance[1]} P点",
                    )
            return

        # 如果该频道未在配置的 group_map 中，直接忽略
        if event.channel.id not in config.group_map:
            logger.warning(
                "收到未配置频道的弹幕，频道 ID: {}",
                event.channel.id,
            )
            return

        # 将 Satori 频道 ID 映射为本地弹幕分组
        danmaku_channel = config.group_map[event.channel.id]

        # 获取发送者用户 ID
        userid = event.user.id
        
        # 尝试获取用户名（优先级：成员昵称 > 用户昵称 > 用户名）
        username = (
            event.member.nick
            or event.user.nick
            or event.user.name
            or "匿名"
        )
        cash_user_id = str(userid) if userid else f"name:{username}"

        room_cash_system.reward_for_message(
            room_id=danmaku_channel,
            user_id=cash_user_id,
            user_name=username,
        )

        # 消息内容是“元素列表”
        elements = event.message.message

        logger.debug(
            "收到弹幕，频道: {}, 用户: {}-{}, 内容: {}",
            danmaku_channel,
            userid,
            username,
            elements,
        )

        # 使用 DanmakuMessage 的工厂方法构造消息对象
        danmaku = DanmakuBuilder.create(
            senderId=str(userid),
            sender=username,
            elements=elements,
            avatar_url=event.user.avatar,
        )

        if danmaku is None:
            logger.warning(
                "无法构建弹幕消息，频道: {}, 用户: {}-{}, 内容: {}",
                danmaku_channel,
                userid,
                username,
                elements,
            )
            return
        else:
            logger.debug(
                "构建弹幕消息成功，频道: {}, 用户: {}-{}, 弹幕对象: {}",
                danmaku_channel,
                userid,
                username,
                danmaku,
            )
            
        if isinstance(danmaku, PlainDanmakuMessage):
            # 检查是否是礼品码
            key = danmaku.text.strip()
            try:
                after_charge = room_cash_system.redeem_giftcode(
                    room_id=danmaku_channel,
                    user_id=cash_user_id,
                    user_name=username,
                    giftcode=key,
                )
            except GiftCodeFormatError:
                # 不是礼品码
                # 当普通弹幕处理
                
                logger.debug(
                    "普通弹幕，频道: {}, 用户: {}-{}, 内容: {}",
                    danmaku_channel,
                    userid,
                    username,
                    danmaku.text,
                )
                
                pass

            except GiftCodeRedeemError as e:
                logger.warning(
                    "礼品码兑换失败，频道: {}, 用户: {}-{}, 礼品码: {}, 错误: {}",
                    danmaku_channel,
                    userid,
                    username,
                    key,
                    e,
                )
                return
            
            else:
                logger.info(
                    "礼品码兑换成功，频道: {}, 用户: {}-{}, 兑换后余额: {} 元, {} 火",
                    danmaku_channel,
                    userid,
                    username,
                    after_charge["yuan"],
                    after_charge["huo"],
                )
                return
                
        if isinstance(danmaku, SuperChatMessage):
            allowed, balance = room_cash_system.spend_huo(
                room_id=danmaku_channel,
                user_id=cash_user_id,
                user_name=username,
                amount=danmaku.cost,
            )
            if not allowed:
                # 则将弹幕替换为普通弹幕
                logger.info(
                    "用户 {}-{} 的余额不足，无法发送特权弹幕，已替换为普通弹幕",
                    userid,
                    username,
                )
                danmaku = DanmakuBuilder.to_plain(danmaku)
                logger.debug(
                    "替换后的普通弹幕对象，频道: {}, 用户: {}-{}, 弹幕对象: {}",
                    danmaku_channel,
                    userid,
                    username,
                    danmaku,
                )
        
        if isinstance(danmaku, GiftMessage):
            allowed, balance = room_cash_system.spend_yuan(
                room_id=danmaku_channel,
                user_id=cash_user_id,
                user_name=username,
                amount=danmaku.cost,
            )
            if not allowed:
                # 则将弹幕替换为普通弹幕
                logger.info(
                    "用户 {}-{} 的余额不足，无法发送特权弹幕，已替换为普通弹幕",
                    userid,
                    username,
                )
                danmaku = DanmakuBuilder.to_plain(danmaku)
                logger.debug(
                    "替换后的普通弹幕对象，频道: {}, 用户: {}-{}, 弹幕对象: {}",
                    danmaku_channel,
                    userid,
                    username,
                    danmaku,
                )

        if isinstance(danmaku, EmoteMessage):
            # 如果是表情消息，尝试缓存表情图片
            emoji_url = await emoji_cache.load_emoji(danmaku.emote_url, userid)
            if emoji_url is None:
                logger.warning(
                    "表情图片缓存失败，频道: {}, 用户: {}-{}, URL: {}",
                    danmaku_channel,
                    userid,
                    username,
                    danmaku.emote_url,
                )
                return
            else:
                logger.debug(
                    "表情图片缓存成功，频道: {}, 用户: {}-{}, URL: {}",
                    danmaku_channel,
                    userid,
                    username,
                    danmaku.emote_url,
                )
                danmaku.emote_url = emoji_url
        elif isinstance(danmaku, (SuperChatMessage, GiftMessage)):
            # 尝试缓存头像图片（如果有）
            if danmaku.avatar_url is not None:
                avatar_url = await emoji_cache.load_emoji(danmaku.avatar_url, userid, ttl_seconds=3600)
                if avatar_url is None:
                    logger.warning(
                        "头像图片缓存失败，频道: {}, 用户: {}-{}, URL: {}",
                        danmaku_channel,
                        userid,
                        username,
                        danmaku.avatar_url,
                    )
                    # 头像缓存失败不影响弹幕展示，继续处理
                else:
                    logger.debug(
                        "头像图片缓存成功，频道: {}, 用户: {}-{}, URL: {}",
                        danmaku_channel,
                        userid,
                        username,
                        danmaku.avatar_url,
                    )
                    danmaku.avatar_url = avatar_url
        
        # 广播前补上上游消息 ID 与成员角色：
        #   message_id —— 撤回 / 贴表情事件要按它对齐"大屏上的这个答案"
        #   is_admin   —— 群主 / 管理员，游戏前端据此自动认定主持人
        # 注意要放在余额不足降级（to_plain 会重建对象）之后，否则会被丢掉
        danmaku.message_id = _sid(event.message) or None
        danmaku.is_admin = member_is_admin(event.member)

        # 将弹幕广播到对应分组的所有连接
        await connection_manager.broadcast_to_group(
            danmaku_channel,
            danmaku,
        )

    @client.register_on(EventType.MESSAGE_DELETED)
    async def handle_message_deleted(account: Account, event):
        # 群消息被撤回（Satori message-deleted）→ 推给大屏，由游戏前端决定怎么处理
        _ = account
        await forward_recall(config, connection_manager, event)

    @client.register_on(EventType.REACTION_ADDED)
    async def handle_message_reaction(account: Account, event):
        # 有人给某条消息贴了表情（Satori reaction-added）→ 推给大屏
        # 游戏侧用它做「答题审核」：主持人给答案贴任意表情 = 这条答案通过
        _ = account
        await forward_reaction(config, connection_manager, event, member_admin, action="add")

    @client.register_on(EventType.REACTION_REMOVED)
    async def handle_message_reaction_removed(account: Account, event):
        # 表情被取消了（Satori reaction-removed）→ 推给大屏
        # 游戏侧理解为「取消审核」：QQ 里贴的表情可以撤掉，撤掉就是主持人反悔了
        _ = account
        await forward_reaction(config, connection_manager, event, member_admin, action="remove")
    
    # =========================
    # 事件探针
    # =========================
    # 兜底登记上游推来的每一类事件：彩排时用
    #   GET /api/danmaku/v1/admin/event-probe?token=<upstream.token>
    # 就能确认 message-deleted / reaction-added 到底有没有来
    async def _event_probe(account: Account, event):
        _ = account
        record_satori_event(event)

    client.register(_event_probe)

    # =========================
    # 获取群组列表
    # =========================
    group_ids = list(config.group_map.keys())
    
    @client.lifecycle
    async def on_ready(account: Account, *args, **kwargs):
        nonlocal users
        
        logger.info("Satori 客户端已连接，正在获取频道列表")
        
        async for guild in account.guild_list():
            if guild.id in group_ids:
                logger.info(
                    "获取到配置的频道，频道 ID: {}, 频道名称: {}",
                    guild.id,
                    guild.name,
                )
                async for user in account.guild_member_list(guild.id):
                    member_user = user.user
                    if member_user is None:
                        continue
                    logger.debug(
                        "获取到频道成员，频道 ID: {}, 用户名: {}",
                        guild.id,
                        member_user.name
                    )
                    users.add(member_user.id)
                    # 顺手缓存群主/管理员身份：贴表情事件本身不带角色，
                    # 游戏侧要靠它判断"这个贴表情的人有没有资格审核"
                    member_admin[member_user.id] = member_is_admin(user)
            else:
                logger.debug(
                    "获取到未配置的频道，频道 ID: {}, 频道名称: {}",
                    guild.id,
                    guild.name,
                )
    
    @client.register_on(EventType.GUILD_MEMBER_ADDED)
    async def handle_member_added(account: Account, event):
        nonlocal users
        
        if event.guild.id in group_ids:
            user = event.user
            if user.id not in users:
                logger.info(
                    "新成员加入配置的频道，频道 ID: {}, 用户名: {}",
                    event.guild.id,
                    user.name,
                )
                users.add(user.id)

    # =========================
    # 启动 Launart 组件
    # =========================

    # 添加 aiohttp 客户端服务（底层网络支持）
    launart.add_component(AiohttpClientService())

    # 添加 Satori 客户端组件
    launart.add_component(client)

    # 启动 Launart，并将其作为一个 asyncio 任务运行
    satori_task = create_task(
        launart.launch(),
        name="satori",
    )

    logger.info("Satori 客户端正在启动")

    return satori_task


async def stop_satori_client():
    """停止 Satori 客户端"""
    global satori_task, launart

    # # 清理全局状态
    # satori_task = None
    # launart = None
    
    # logger.info("Satori 客户端已停止（真实停止不在此log）")
    
    # 如果客户端正在运行，则触发关闭流程
    if satori_task is not None and launart is not None:
        # 向 Launart 发送系统信号，触发组件优雅退出
        launart._on_sys_signal(
            None,
            None,
            main_task=satori_task,
        )

        # 等待任务结束
        await satori_task

        # 清理全局状态
        satori_task = None
        launart = None

        logger.info("Satori 客户端已停止")
