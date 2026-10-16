"""群主 / 管理员识别（is_admin）的回归测试。

背景（现场反馈）："我是 874090738 的群主，但游戏里没被当成管理员"。

查下来的原因：LLBot 的 Satori 实现里，成员角色是这样给的 ——

    roles: [{ id: data.role.toString(), name: { [GroupMemberRole.Owner]: "owner", ... } }]

角色 id 取的是 QQ 角色枚举（4=群主、3=管理员、2=成员）。但**群消息载荷里的 member
往往不带真实 role**（`data.role` 是 undefined → id 变成 "undefined"），于是
``member.roles`` 字段在、id 却认不出来，群主被判成普通成员 ✗。

而贴表情事件根本没有 member，从来只能靠"Satori 连上时用 guild.member.list 建好的缓存"。
本测试锁死两层兜底的行为：事件角色优先，认不出来再查缓存；且缓存不会被降级。
"""

from __future__ import annotations

from nekocast_danmaku.danmaku.game_events import (
    member_is_admin,
    onebot_role_is_admin,
    resolve_is_admin,
    role_id_name,
    role_name_of,
)


class Role:
    def __init__(self, rid, name=None):
        self.id = rid
        self.name = name


class Member:
    def __init__(self, roles):
        self.roles = roles


# LLBot 的 name 是「角色 id → 角色名」的映射表（不是单个字符串）
LLBOT_NAME_TABLE = {"4": "owner", "3": "admin", "2": "member", "0": "notApplicable"}

OWNER = Member([Role("4", dict(LLBOT_NAME_TABLE))])
ADMIN = Member([Role("3", dict(LLBOT_NAME_TABLE))])
NORMAL = Member([Role("2", dict(LLBOT_NAME_TABLE))])
# ★ 现场那种：消息载荷里 role 丢了
BROKEN = Member([Role("undefined", dict(LLBOT_NAME_TABLE))])

UID = "2161299906"


# --------------------------- 角色名 / id 的解析 ---------------------------

def test_llbot_name_table_is_looked_up_by_role_id():
    """LLBot 的 name 是 dict，必须按该成员自己的 role.id 去查"""
    assert role_name_of(Role("4", dict(LLBOT_NAME_TABLE))) == "owner"
    assert role_name_of(Role("3", dict(LLBOT_NAME_TABLE))) == "admin"
    assert role_name_of(Role("2", dict(LLBOT_NAME_TABLE))) == "member"
    # 查不到时留空，交给 id 兜底
    assert role_name_of(Role("9", dict(LLBOT_NAME_TABLE))) == ""


def test_plain_string_role_name_still_works():
    """非 LLBot 的上游直接给字符串角色名"""
    assert role_name_of(Role("x", "Owner")) == "owner"
    assert role_name_of(Role("x", " 管理员 ")) == "管理员"


def test_qq_role_id_fallback():
    """角色名缺失/不认识时按 QQ 枚举兜底"""
    assert role_id_name(Role("4")) == "owner"
    assert role_id_name(Role("3")) == "admin"
    assert role_id_name(Role("2")) == ""
    assert role_id_name(Role("undefined")) == ""


# --------------------------- 事件自带角色 ---------------------------

def test_admin_detected_from_event_roles():
    assert member_is_admin(OWNER) is True
    assert member_is_admin(ADMIN) is True
    assert member_is_admin(NORMAL) is False


def test_event_member_missing_or_empty():
    assert member_is_admin(None) is False
    assert member_is_admin(Member(None)) is False
    assert member_is_admin(Member([])) is False


def test_broken_message_payload_alone_is_not_enough():
    """★ 消息载荷里角色丢了 —— 光靠事件判断不出来（这正是 bug 的现场）"""
    assert member_is_admin(BROKEN) is False


def test_onebot_role_is_admin():
    assert onebot_role_is_admin("owner") is True
    assert onebot_role_is_admin("ADMIN") is True
    assert onebot_role_is_admin("member") is False
    assert onebot_role_is_admin(None) is False


# --------------------------- 两层兜底：resolve_is_admin ---------------------------

def test_resolve_prefers_event_role():
    assert resolve_is_admin(OWNER, None, UID) is True
    assert resolve_is_admin(OWNER, {UID: False}, UID) is True   # 事件说了算
    assert resolve_is_admin(NORMAL, {UID: True}, UID) is True   # 缓存兜底


def test_resolve_falls_back_to_cache_when_payload_is_broken():
    """★ 这就是修复的核心：群消息载荷认不出来时，用连接时建好的缓存"""
    assert resolve_is_admin(BROKEN, {UID: True}, UID) is True
    assert resolve_is_admin(None, {UID: True}, UID) is True      # 贴表情事件没有 member


def test_resolve_tolerates_int_or_str_keys():
    assert resolve_is_admin(BROKEN, {int(UID): True}, UID) is True
    assert resolve_is_admin(BROKEN, {UID: True}, int(UID)) is True


def test_resolve_is_safe_without_cache():
    assert resolve_is_admin(BROKEN, None, UID) is False
    assert resolve_is_admin(BROKEN, {}, UID) is False
    assert resolve_is_admin(BROKEN, {UID: True}, None) is False
    assert resolve_is_admin(None, None, None) is False


def test_ratchet_never_downgrades_known_admin():
    """缓存只升不降：某条消息里没有角色，不能把已知的管理员抹掉"""
    cache: dict = {}
    if resolve_is_admin(OWNER, cache, UID):
        cache[str(UID)] = True
    assert cache[str(UID)] is True
    # 随后来一条"角色丢了"的消息，缓存照样是 True
    assert resolve_is_admin(BROKEN, cache, UID) is True


# --------------------------- 群 id 形态容错 ---------------------------

def test_same_guild_id_tolerates_formats():
    from nekocast_danmaku.danmaku.game_events import same_guild_id

    groups = ["874090738", "767341961"]
    assert same_guild_id("874090738", groups) is True
    assert same_guild_id(874090738, groups) is True            # int
    assert same_guild_id(" 874090738 ", groups) is True        # 空格
    assert same_guild_id("2|874090738", groups) is True        # LLBot 的平台前缀
    assert same_guild_id("2|874090738|", groups) is True
    assert same_guild_id("1105708163", groups) is False
    assert same_guild_id(None, groups) is False
    # group_map 的 key 带前缀时也能匹配上
    assert same_guild_id("874090738", ["2|874090738"]) is True


# --------------------------- OneBot 版：role → 缓存 ---------------------------

def test_onebot_resolve_role_then_cache():
    from nekocast_danmaku.danmaku.game_events import resolve_admin_from_role

    # 事件自带角色：直接认
    assert resolve_admin_from_role("owner", None, "1") is True
    assert resolve_admin_from_role("admin", None, "1") is True
    assert resolve_admin_from_role("member", None, "1") is False
    # 事件没角色（贴表情事件就是这种）：查缓存
    assert resolve_admin_from_role(None, {"1": True}, "1") is True
    assert resolve_admin_from_role(None, {"1": True}, "2") is False
    # int / str 两种 key 都认
    assert resolve_admin_from_role(None, {1: True}, 1) is True
    assert resolve_admin_from_role(None, {1: True}, "1") is True
    # 没有缓存也不能崩
    assert resolve_admin_from_role(None, None, None) is False
