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
from .game_events import (
    member_is_admin,
    recall_frame,
    reaction_frame,
    resolve_is_admin,
    same_guild_id,
)
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


async def refresh_member_admins(account: Account, config, member_admin: dict, reason: str = "") -> None:
    """群角色变了 → **强制重算**管理员名单（先清空，再按各配置群的成员列表重建）。

    必须清空：缓存是"只升不降"的并集，不清的话"撤掉管理员"永远不生效。
    "设/撤管理员"和"换群主"都会带出成员更新事件，所以一次完整重算就都覆盖了。
    """
    if member_admin is None:
        return
    # ⚠ 先算好一份新的，最后一步再原子替换：
    #   原来是先 clear() 再跨多个 await 逐群重建，那几秒里所有人的 is_admin 都是 False，
    #   正好在这时候贴表情的主持人会被前端判成"非主持人"，审核静默丢失。
    fresh: dict = {}
    admins = 0
    try:
        async for guild in account.guild_list():
            if not same_guild_id(guild.id, config.group_map.keys()):
                continue
            async for user in account.guild_member_list(guild.id):
                member_user = user.user
                if member_user is None:
                    continue
                if member_is_admin(user):
                    fresh[member_user.id] = True
                    admins += 1
    except Exception as exc:  # noqa: BLE001 —— 重算失败也不能把客户端带崩
        logger.warning("重算管理员名单出错（忽略）：{}", exc)
    member_admin.clear()
    member_admin.update(fresh)
    logger.info("群角色变动（{}）→ 管理员名单已强制重算：{} 人", reason or "未知", admins)

async def _remember_reactor_admin(config, account: Account, event, member_admin: dict) -> None:
    """贴表情事件不带角色：缓存里没有这个人时，按需问一次上游（与消息路径一致）。

    ⚠ 弹幕姬同时挂在多个群上：只在**本机配置了的群**里做这件事 ——
    别的群的表情事件既不查询、也不进角色缓存（免得缓存被无关的人塞满）。

    现场踩过：群主只贴表情、没在群里发过言，缓存里就没有他，于是"非主持人贴的表情不算审核"。
    """
    if not member_admin:
        return
    channel = getattr(event, "channel", None)
    channel_id = getattr(channel, "id", None) if channel is not None else None
    if not channel_id:
        return
    # 认群口径与 group_map 一致（走字符串比较，容忍类型/前后缀差异）
    if not _group_configured(config, channel_id):
        return
    user = getattr(event, "user", None)
    uid = getattr(user, "id", None) if user is not None else None
    if not uid or str(uid) in member_admin:
        return
    # LLBot 里群号既是 guild 也是 channel；有 guild 就用 guild
    guild = getattr(event, "guild", None)
    guild_id = getattr(guild, "id", None) if guild is not None else None
    guild_id = guild_id or channel_id
    try:
        member = await account.guild_member_get(str(guild_id), str(uid))
        if member_is_admin(member):
            # 只升不降：他在别的群里是管理员也算数（管理员 = 所有配置群管理员的并集）
            member_admin[str(uid)] = True
            logger.info("按需查到 {} 是群主/管理员（群 {}）", uid, guild_id)
    except Exception as exc:  # noqa: BLE001 —— 上游没实现/抖动都不该影响审核帧下发
        logger.debug("按需查询成员角色失败（忽略）：{}", exc)
    if not member_admin.get(str(uid)):
        # 记下"问过了、不是管理员"：否则同一个普通成员每贴一次表情都要再问一次上游。
        # 只在当前不是 True 时才写 —— 绝不覆盖别的群认下的 True（并集语义）。
        member_admin[str(uid)] = False


def _group_configured(config, group_id) -> bool:
    """这个群在不在本机配置里（弹幕姬挂着多个群，只认配置过的）"""
    try:
        return same_guild_id(group_id, config.group_map.keys())
    except Exception:  # noqa: BLE001
        return str(group_id).strip() in {str(k).strip() for k in config.group_map.keys()}

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
        frame["is_admin"] = resolve_is_admin(None, member_admin, frame["sender_id"])
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
        # is_admin：优先用事件自带的角色；LLBot 的群消息载荷里那个角色常常不可靠
        # （transformGroupMember 依赖 data.role，消息里可能是 undefined），
        # 所以再兜一层「连上时用 guild.member.list 建好的角色缓存」——贴表情那条路一直这么用。
        danmaku.is_admin = resolve_is_admin(event.member, member_admin, userid)
        # 反向补缓存：只在确认是管理员时写 True，绝不把已知的管理员降级
        # （角色载荷时有时无，不能因为某条消息里没有角色就抹掉身份）
        if danmaku.is_admin:
            member_admin[str(userid)] = True
        # 兜底三：缓存里也没有这个人时，按需向上游问一次（不依赖 guild.list ——
        # 有些 Satori 实现（LLBot 是薄壳）根本没实现它，启动时那份成员缓存可能是空的）。
        # 问到就写进缓存，之后不再重复问；上游不实现这个接口也不能影响正常发弹幕。
        if not danmaku.is_admin and userid is not None and str(userid) not in member_admin:
            _guild_id = getattr(getattr(event, "guild", None), "id", None) or event.channel.id
            try:
                _member = await account.guild_member_get(str(_guild_id), str(userid))
                if member_is_admin(_member):
                    # 只升不降：并集语义（他在任何一个配置群里是管理员就算管理员）
                    member_admin[str(userid)] = True
                    logger.info("按需查到 {} ({}) 是群主/管理员（频道 {}）", username, userid, _guild_id)
            except Exception as exc:  # noqa: BLE001 —— 上游没实现/抖动都不该影响弹幕
                logger.debug("按需查询成员角色失败（忽略）：{}", exc)
            if not member_admin.get(str(userid)):
                # 记下"问过了、不是管理员"：否则同一个人每发一条弹幕都要再问一次上游（每条都要多等一次 HTTP）。
                # 只在当前不是 True 时才写 —— 并集语义不能破。
                member_admin[str(userid)] = False
            danmaku.is_admin = bool(member_admin.get(str(userid), False))

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
        # 游戏侧用它做「答题审核」：主持人/管理员给答案贴任意表情 = 这条答案通过
        await _remember_reactor_admin(config, account, event, member_admin)
        await forward_reaction(config, connection_manager, event, member_admin, action="add")

    @client.register_on(EventType.REACTION_REMOVED)
    async def handle_message_reaction_removed(account: Account, event):
        # 表情被取消了（Satori reaction-removed）→ 推给大屏
        # 游戏侧理解为「取消审核」：QQ 里贴的表情可以撤掉，撤掉就是主持人反悔了
        _ = account
        # 取消表情也算审核动作，同样先补一次按需查询（否则缓存里没有的人永远是"非主持人"）
        await _remember_reactor_admin(config, account, event, member_admin)
        await forward_reaction(config, connection_manager, event, member_admin, action="remove")

    @client.register_on(EventType.GUILD_MEMBER_UPDATED)
    async def handle_member_updated(account: Account, event):
        # 成员资料/角色变了（设管理员、撤管理员、换群主都会走到这里）→ 强制重算
        await refresh_member_admins(account, config, member_admin, "guild-member-updated")

    @client.register_on(EventType.GUILD_ROLE_UPDATED)
    async def handle_role_updated(account: Account, event):
        # 角色定义变了（权限/名称）→ 同样重算一遍，宁可多算
        await refresh_member_admins(account, config, member_admin, "guild-role-updated")
    
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
    # 群 id 统一成字符串再比较：不同上游可能给 int、或带空格/平台前缀，
    # 直接 `in` 会让整个群的成员缓存都不建（于是群主也认不出来）
    group_ids = {str(k).strip() for k in config.group_map.keys()}
    
    @client.lifecycle
    async def on_ready(account: Account, *args, **kwargs):
        nonlocal users
        
        logger.info("Satori 客户端已连接，正在获取频道列表")
        
        async for guild in account.guild_list():
            logger.info(
                "Satori 群列表：id={!r} name={!r} 是否在 group_map 里={}",
                guild.id,
                getattr(guild, "name", None),
                same_guild_id(guild.id, group_ids),
            )
            if same_guild_id(guild.id, group_ids):
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
                    # ⚠ 管理员 = **所有配置群里管理员/群主的并集**（只升不降）：
                    #   他在 A 群是群主、在 B 群只是普通成员时，绝不能被 B 群这一轮覆盖成 False。
                    if member_is_admin(user) and not member_admin.get(member_user.id):
                        member_admin[member_user.id] = True
                        # 建缓存时把管理员一个个列出来：现场出问题时一眼就能看到"到底认了谁"
                        logger.info(
                            "频道 {} 成员 {} ({}) 是群主/管理员",
                            guild.id,
                            member_user.name,
                            member_user.id,
                        )
            else:
                logger.debug(
                    "获取到未配置的频道，频道 ID: {}, 频道名称: {}",
                    guild.id,
                    guild.name,
                )
    
    @client.register_on(EventType.GUILD_MEMBER_ADDED)
    async def handle_member_added(account: Account, event):
        nonlocal users
        
        if same_guild_id(event.guild.id, group_ids):
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
