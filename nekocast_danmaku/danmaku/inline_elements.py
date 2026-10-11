# -*- coding: utf-8 -*-
"""行内元素（inline element）解析。

上游（LLOneBot 等 OneBot 实现）会把无法用纯文本表达的段塞进消息文本里，形如：

    <face id="339" type="2" platform="llonebot"/>

它们和普通文字混在一起（``哈哈哈哈<face id="339" type="2" platform="llonebot"/>``），
对显示层来说是"文字里插了张图"，对游戏这类严肃消费者来说则是**非法内容**
（不能让玩家把标签塞进答案里带到屏幕上）。

这里提供三件事：

* :func:`parse_inline_elements` —— 把文本切成一串元素；
* :func:`segments` —— 切成"文字段 + 元素段"，给前端行内渲染用；
* :func:`to_plain_text` —— 把元素整体替换成 ``█``（游戏侧要的"非法内容替换"）。

表情图地址见 :func:`face_image_url`：QQ 系统表情在腾讯的老地址上，
其中"会动的"那种（``type="2"`` / id ≥ 222）编号是 ``10000+id``；而 QQ"回应"那类
标准 emoji 的 id 直接是 Unicode 码点，走 Twemoji。两种都返回主地址，
前端拿不到图时自己回退到另一个 / 退回 ``█``。
"""
from __future__ import annotations

import re
from typing import Any

# 已知的行内元素名（LLOneBot / OneBot 生态里会出现的那些）
INLINE_ELEMENT_NAMES = (
    "face", "mface", "marketface", "image", "record", "voice", "video", "file",
    "json", "xml", "reply", "at", "poke", "forward", "node", "markdown",
)

INLINE_PATTERN = re.compile(
    r"<(?P<name>" + "|".join(INLINE_ELEMENT_NAMES) + r")\b(?P<attrs>[^<>]*)/?>",
    re.IGNORECASE,
)

ATTR_PATTERN = re.compile(r'([A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)"')

# 非法内容统一替换成这个字符（整块方块，看得见、又不会被误当成正常文字）
PLACEHOLDER = "█"

EMOJI_UNICODE_MIN = 0x1F000
EMOJI_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{code}.png"
FACE_IMG = "https://qzonestyle.gtimg.cn/qzone/em/e{id}.gif"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def parse_inline_elements(text: Any) -> list[dict]:
    """把文本里的行内元素抽出来（按出现顺序）

    每项形如::

        {"name": "face", "raw": '<face id="339" .../>', "start": 4, "end": 41,
         "attrs": {"id": "339", "type": "2", "platform": "llonebot"}}
    """
    raw = _text(text)
    if not raw or "<" not in raw:
        return []
    found: list[dict] = []
    for m in INLINE_PATTERN.finditer(raw):
        attrs = {k.lower(): v for k, v in ATTR_PATTERN.findall(m.group("attrs") or "")}
        found.append({
            "name": m.group("name").lower(),
            "raw": m.group(0),
            "start": m.start(),
            "end": m.end(),
            "attrs": attrs,
        })
    return found


def face_image_url(face_id: Any, face_type: Any = None) -> str:
    """表情图地址（主地址）

    * id 是 Unicode 码点（QQ"回应"那类）→ Twemoji；
    * 否则是经典 QQ 表情：``type="2"`` 或 id ≥ 222 的是"会动的"，
      腾讯编号为 ``10000+id``（实测 e419 是 404、e10419 才是 200）。
    """
    text = _text(face_id).strip()
    if not text:
        return ""
    if not text.isdigit():
        return ""
    code = int(text)
    if EMOJI_UNICODE_MIN <= code <= 0x10FFFF:
        return EMOJI_CDN.format(code="%x" % code)
    animated = _text(face_type).strip() == "2" or code >= 222
    return FACE_IMG.format(id=(10000 + code if animated else code))


def face_image_url_alt(face_id: Any, face_type: Any = None) -> str:
    """表情图备用地址（前端 onerror 时换这个再试）"""
    text = _text(face_id).strip()
    if not text.isdigit():
        return ""
    code = int(text)
    if EMOJI_UNICODE_MIN <= code <= 0x10FFFF:
        return ""
    animated = _text(face_type).strip() == "2" or code >= 222
    return FACE_IMG.format(id=(code if animated else 10000 + code))


def segments(text: Any) -> list[dict]:
    """切成"文字段 + 元素段"，给前端行内渲染用

    文字段::

        {"kind": "text", "text": "哈哈哈"}

    元素段（face / mface 会带 url；其它类型只有名字和原始文本）::

        {"kind": "face", "name": "face", "id": "339", "type": "2",
         "url": "...", "url_alt": "...", "raw": '<face id="339" type="2" .../>'}
        {"kind": "element", "name": "image", "attrs": {...}, "raw": "<image .../>"}

    注意：``platform`` 之类的附加属性**不做处理**（只出现在 attrs / raw 里），
    渲染只认 id 与 type。
    """
    raw = _text(text)
    if not raw:
        return []
    found = parse_inline_elements(raw)
    if not found:
        return [{"kind": "text", "text": raw}]

    out: list[dict] = []
    cursor = 0
    for item in found:
        if item["start"] > cursor:
            out.append({"kind": "text", "text": raw[cursor:item["start"]]})
        attrs = item["attrs"]
        if item["name"] in ("face", "mface"):
            seg = {
                "kind": "face",
                "name": item["name"],
                "id": attrs.get("id", ""),
                "type": attrs.get("type", ""),
                "url": face_image_url(attrs.get("id"), attrs.get("type")),
                "url_alt": face_image_url_alt(attrs.get("id"), attrs.get("type")),
                "raw": item["raw"],
            }
        else:
            seg = {"kind": "element", "name": item["name"], "attrs": attrs, "raw": item["raw"]}
        out.append(seg)
        cursor = item["end"]
    if cursor < len(raw):
        out.append({"kind": "text", "text": raw[cursor:]})
    return out


def to_plain_text(text: Any, replacement: str = PLACEHOLDER) -> str:
    """把行内元素整体替换成 ``replacement``（默认为 █）

    游戏侧只认"纯文本"：玩家不能靠标签把图片/卡片塞到屏幕上。
    注意"整条只有元素"的消息**也算数**（答案就是那个 █），不做额外拒绝。
    """
    raw = _text(text)
    if not raw or "<" not in raw:
        return raw
    return INLINE_PATTERN.sub(replacement, raw)


def has_inline_element(text: Any) -> bool:
    return bool(parse_inline_elements(text))
