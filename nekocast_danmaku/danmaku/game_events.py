"""游戏侧事件帧（撤回 / 贴表情）的线格式定义。

大屏游戏前端（danmaku-app 的 fourth_righter）按 ``type`` 分流这些帧：

.. code-block:: text

    # 群消息被撤回
    {type: "recall",   message_id, sender_id, operator_id, channel_id}

    # 有人给某条消息贴了表情（游戏侧用作「答题审核」）；
    # action=remove 表示取消了表情（游戏侧理解为「取消审核」）
    # emoji 是上游的表情 id，emoji_char/emoji_url 是换算好的字符与图片地址
    {type: "reaction", action: "add"|"remove", message_id, sender_id,
     emoji, emoji_char, emoji_url, channel_id}

``message_id`` 必须与弹幕帧里的 ``message_id`` 同源，前端才能把
「群里的那条消息」对回「大屏上的那个答案」。

另有身份辅助：群主 / 管理员要自动成为游戏主持人，因此弹幕帧里会带上
``is_admin``（见 danmaku_message.DanmakuBase）。
"""

# 群主 / 管理员在各上游里的角色名（Satori 用 roles[].name，OneBot 用 sender.role）
ADMIN_ROLE_NAMES = {"owner", "admin"}

from .inline_elements import face_image_url, face_image_url_alt  # noqa: E402  （表情图地址统一在那边）

# QQ 群成员角色 id（LLBot / NTQQ 枚举）：0 不适用、2 普通成员、3 管理员、4 群主。
# 仅在角色名缺失或不认识时按 id 兜底。
QQ_ROLE_ID_NAMES = {"3": "admin", "4": "owner"}


def role_name_of(role) -> str:
    """从 Satori 的 Role 对象取角色名。

    ⚠️ LLBot 的 ``role.name`` 不是字符串，而是「角色 id → 角色名」的本地化映射表
    （形如 ``{"4": "owner", "3": "admin", "2": "member", "0": ...}``），
    satori-python 会把它原样保留成 dict。**必须按该成员自己的 role.id 去查**，
    不能随手取表里的一个值 —— 那张表里 owner/admin/member 全都有，
    取错了会把所有人都判成管理员。
    """
    name = getattr(role, "name", None)
    if isinstance(name, dict):
        role_id = getattr(role, "id", None)
        if role_id is not None:
            value = name.get(str(role_id))
            if isinstance(value, str):
                return value.strip().lower()
        return ""
    if isinstance(name, str):
        return name.strip().lower()
    return ""


def role_id_name(role) -> str:
    """按 QQ 角色 id 兜底取名（角色名缺失时用）"""
    return QQ_ROLE_ID_NAMES.get(str(getattr(role, "id", "")), "")


def member_is_admin(member) -> bool:
    """Satori MessageEvent.member → 是否群主/管理员"""
    if member is None:
        return False
    for role in getattr(member, "roles", None) or []:
        if role_name_of(role) in ADMIN_ROLE_NAMES:
            return True
        if role_id_name(role) in ADMIN_ROLE_NAMES:
            return True
    return False


def onebot_role_is_admin(role) -> bool:
    """OneBot 11 的 ``sender.role``（"owner" / "admin" / "member"）→ 是否主持人"""
    return isinstance(role, str) and role.strip().lower() in ADMIN_ROLE_NAMES

def resolve_admin_from_role(role, member_admin=None, user_id=None) -> bool:
    """OneBot 版判断：``sender.role``（owner/admin/member）优先，其次查缓存。

    很多 OneBot 事件（贴表情、撤回）**不带**操作者的角色，只有 user_id ——
    所以要在消息事件里把管理员身份记进 ``member_admin``，这里再兜一层。
    """
    if onebot_role_is_admin(role):
        return True
    if not member_admin or user_id is None:
        return False
    # int / str 两种 key 形态都试一遍（上游给的是数字还是字符串并不稳定）
    if member_admin.get(user_id) or member_admin.get(str(user_id)):
        return True
    try:
        return bool(member_admin.get(int(user_id)))
    except (TypeError, ValueError):
        return False


def same_guild_id(raw_id, group_ids) -> bool:
    """比较群 id 时容忍各种写法。

    各上游给的群 id 形态并不统一：可能是 int、带前后空格，也可能带平台前缀
    （LLBot 的消息 id 就长成 ``2|874090738|1151``）。直接 `in` 会让整个群的
    成员缓存都不建，于是群主也认不出来。
    """
    def norm(value) -> str:
        # 群号的位置：裸号 "874090738" 取第 1 段；带平台前缀的 "2|874090738" 或
        #   "2|874090738|1151"（平台|群号|消息序号）都要取**第 2 段**。
        # ⚠ 不能取最后一段：三段式会取成消息序号，认群直接失效（审计发现）。
        parts = [p.strip() for p in str(value).split("|") if p.strip()]
        if not parts:
            return ""
        return parts[1] if len(parts) >= 2 else parts[0]

    return norm(raw_id) in {norm(k) for k in group_ids}


def resolve_is_admin(member, member_admin=None, user_id=None) -> bool:
    """综合两层来源判断某人是不是群主/管理员。

    1. 事件自带的 ``member.roles``（Satori 的 MessageEvent.member）；
    2. ``member_admin`` 缓存 —— Satori 连上时用 ``guild.member.list`` 建好的
       「用户 id → 是否管理员」。

    为什么必须有两层：LLBot 的 ``transformGroupMember`` 用 ``data.role.toString()``
    当角色 id，而**群消息载荷里的 member 往往不带真实 role**（拿到 "undefined"）——
    于是 ``roles`` 字段在、但 id 认不出来，群主会一直被当成普通成员。
    贴表情事件更是完全没有 member，从来只能靠缓存。这里统一两条路。
    """
    if member is not None and member_is_admin(member):
        return True
    if not member_admin or user_id is None:
        return False
    # 上游给的是数字还是字符串并不稳定：str / int 两种 key 形态都试一遍
    if member_admin.get(user_id) or member_admin.get(str(user_id)):
        return True
    try:
        return bool(member_admin.get(int(user_id)))
    except (TypeError, ValueError):
        return False


def _text(value) -> str:
    return "" if value is None else str(value)


def recall_frame(message_id, sender_id="", operator_id="", channel_id="") -> dict:
    return {
        "type": "recall",
        "message_id": _text(message_id),
        "sender_id": _text(sender_id),      # 原消息作者
        "operator_id": _text(operator_id),  # 撤回操作者（可能是管理员）
        "channel_id": _text(channel_id),
    }


# QQ 的「贴表情 / 回应」用的就是标准 emoji 那一套，id 直接就是 **Unicode 码点**：
#   128077 = U+1F44D 👍、128514 = U+1F602 😂、128064 = U+1F440 👀
# 而经典 QQ 表情（微笑、/火车 那些，LLBot 里 face_config 的 454 个）是另一套小编号。
# 图地址的换算和消息里的行内元素（<face id=".."/>）是同一套规则，统一放在 inline_elements。
EMOJI_UNICODE_MIN = 0x1F000     # 标准 emoji 的码点起点（emoji_char 用）


def emoji_char(emoji) -> str:
    """表情 id → 可直接显示的字符（拿不到就返回空串）

    标准 emoji 直接 chr(码点)；上游万一直接给的就是字符，原样返回。
    """
    text = _text(emoji)
    if not text:
        return ""
    if not text.isdigit():
        return text if not text.isascii() else ""
    try:
        code = int(text)
    except ValueError:
        return ""
    if EMOJI_UNICODE_MIN <= code <= 0x10FFFF:
        try:
            return chr(code)
        except ValueError:
            return ""
    return ""


def emoji_image_url(emoji) -> str:
    """表情 id → 图片地址（规则见 inline_elements.face_image_url：标准 emoji 走 Twemoji，
    经典 QQ 表情走腾讯的 e{id}.gif，其中"会动的"编号是 10000+id）"""
    return face_image_url(emoji)


def emoji_image_url_alt(emoji) -> str:
    """表情图备用地址（前端 onerror 时换这个再试）"""
    return face_image_url_alt(emoji)


def reaction_frame(message_id, sender_id="", emoji="", channel_id="", action="add") -> dict:
    """贴表情 / 取消表情。``action`` 取 ``"add"`` 或 ``"remove"``。

    游戏侧把 add 当「通过审核」，remove 当「取消审核」——QQ 里贴的表情
    是可以撤掉的，撤掉就意味着主持人反悔了。

    另外把表情 id 换算成 **字符** 和 **图片地址** 一起带上（见 emoji_char /
    emoji_image_url）：字符在任何机器上都能直接显示，图片用来统一观感。
    """
    return {
        "type": "reaction",
        "action": _text(action) or "add",
        "message_id": _text(message_id),
        "sender_id": _text(sender_id),  # 贴（或取消）表情的人
        "emoji": _text(emoji),          # 上游给的表情 id（QQ 的"回应"就是 Unicode 码点）
        "emoji_char": emoji_char(emoji),
        "emoji_url": emoji_image_url(emoji),
        "channel_id": _text(channel_id),
    }
