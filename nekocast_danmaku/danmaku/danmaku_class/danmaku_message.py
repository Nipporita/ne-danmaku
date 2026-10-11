from pydantic import BaseModel, Field
from typing import Literal, Annotated

# =========================
# blocked 的来源
# =========================
# ``blocked`` 只说"这条消息被拦了"，看不出为什么。下面这些原因写进
# ``block_reasons``，把「人黑名单」和「言论黑名单」区分开：
#   - 人黑名单：这个发送者在禁止名单里 → 他的**所有**消息都被标记
#   - 言论黑名单：这条消息的文本命中正则 → 只标记这一条
#   - 去重：短时间内内容完全相同的重复弹幕
# 注意：被标记的消息**不会被丢弃**，仍会广播（由前端决定是否展示）。
BLOCK_REASON_USER = "user_blacklist"      # 发送者在禁止用户名单里
BLOCK_REASON_WORD = "word_blacklist"      # 文本命中言论黑名单正则
BLOCK_REASON_DUPLICATE = "duplicate"      # 短时间内的重复弹幕

BLOCK_REASONS = (BLOCK_REASON_USER, BLOCK_REASON_WORD, BLOCK_REASON_DUPLICATE)


class DanmakuBase(BaseModel):
    """弹幕消息结构基类，不应直接实例化"""

    # text: str                     # 弹幕内容
    # color: str | None = None      # 颜色（如 #ff0000）
    # size: int | None = None       # 字号（可选）
    senderId: str | None = None   # 发送者唯一 ID（如用户 ID）
    sender: str | None = None     # 发送者昵称
    is_special: bool = False      # 是否为特殊弹幕（如加标识）
    type: str                     # 弹幕类型标识符
    blocked: bool = False         # 是否被黑名单/去重标记（消息仍会广播，由前端决定是否展示）
    block_reasons: list[str] = Field(default_factory=list)  # 被标记的原因，见下方 BLOCK_REASON_*
    message_id: str | None = None # 上游消息 ID：撤回/贴表情要靠它把"群里那条消息"对回"大屏上这条弹幕"
    is_admin: bool = False        # 发送者是否为本群群主/管理员（游戏前端据此自动认定主持人）
    # 行内元素（上游把表情之类的段塞进文本里，形如 <face id="339" type="2" platform="llonebot"/>）：
    #   inline      —— 切成"文字段 + 元素段"，给显示层做行内渲染
    #   text_plain  —— 元素整体替换成 █，给游戏这类"只认纯文本"的消费者用
    #                  （整条只有元素的消息也算数：纯文本就是 █）
    # 见 danmaku/inline_elements.py；platform 之类的附加属性不参与处理
    inline: list[dict] = Field(default_factory=list)
    text_plain: str = ""

    class Config:
        """Pydantic 配置"""
        extra = "forbid"  # 禁止吞字段

    def mark_blocked(self, reason: str) -> None:
        """标记为 blocked 并记下原因（同一原因只记一次；不改变"消息仍会广播"的约定）"""
        self.blocked = True
        if reason and reason not in self.block_reasons:
            self.block_reasons.append(reason)

# =========================
# 弹幕消息本体
# =========================

class PlainDanmakuMessage(DanmakuBase):
    type: Literal["plain"] = "plain"  # 弹幕类型
    
    text: str                     # 弹幕内容
    color: str | None = None      # 颜色（如 #ff0000）
    size: int | None = None       # 字号（可选）
    position: Literal["top", "bottom", "scroll"] = 'scroll'  # 位置（顶部、底部、滚动）

# =========================
# 表情包消息结构
# =========================

class EmoteMessage(DanmakuBase):
    type: Literal["emote"] = "emote"  # 弹幕类型
    
    """表情包消息结构（继承自 DanmakuMessage）"""
    emote_url: str               # 表情包 URL

class MultiEmoteMessage(DanmakuBase):
    type: Literal["multi_emote"] = "multi_emote"  # 弹幕类型
    
    """多表情包消息结构（继承自 DanmakuMessage）"""
    emote_urls: list[str]       # 多个表情包 URL 列表

# ==========================
# SuperChat消息结构（不考虑Bilibili）
# ==========================

class SuperChatMessage(DanmakuBase):
    type: Literal["superchat"] = "superchat"  # 弹幕类型
    text: str                     # 弹幕内容
    avatar_url: str | None = None    # 头像 URL（可选）
    
    """超级聊天消息结构（继承自 DanmakuMessage）"""
    duration: int               # 显示时长（秒）
    cost: float                 # 付费金额

# ==========================
# 礼物消息结构（不考虑Bilibili）
# ==========================

class GiftMessage(DanmakuBase):
    """礼物消息结构（继承自 DanmakuMessage）"""
    type: Literal["gift"] = "gift"  # 弹幕类型
    avatar_url: str | None = None    # 头像 URL（可选）
    image_url: str | None = None     # 礼物图片 URL（可选）
    
    # gift_name: Literal[...]（未来方向）
    gift_name: str             # 礼物名称
    quantity: int              # 礼物数量
    cost: float   # 礼物总价值


DanmakuMessage = Annotated[
    PlainDanmakuMessage |
    EmoteMessage |
    SuperChatMessage |
    GiftMessage,
    Field(discriminator="type"),
]