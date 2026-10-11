"""事件探针：统计上游到底推了哪些事件。

**为什么需要它**：③（监听撤回）和 ④（贴表情审核）都依赖上游把
``message-deleted`` / ``reaction-added`` 真的推下来 —— 这件事在离线环境没法确认，
只能靠彩排时现场看一眼。有了这个探针，彩排时请求

    GET /api/danmaku/v1/admin/event-probe?token=<upstream.token>

就能看到"这类事件到底来没来、最后一次是什么时候、长什么样、后端认不认识它"，
不必去翻日志，也不必猜。

判断口径：
- ``handled: true``  —— 这个类型后端有对应处理器（例如 message-created）
- ``handled: false`` —— 上游推了，但后端没处理（这正是要发现的东西：
  比如 ``guild-member-updated`` 来了但我们没用它）
"""

import threading
import time
from collections import OrderedDict, deque
from typing import Any

# 后端注册过处理器的类型（由各上游客户端启动时登记）
HANDLED_TYPES: set[str] = set()

_MAX_TYPES = 50  # 最多记这么多类型，超出丢最早的（防止上游乱推把内存撑爆）
_RECENT_MAX = 20  # 最近事件留这么多条

_lock = threading.Lock()
_stats: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_recent: deque = deque(maxlen=_RECENT_MAX)


def mark_handled(*type_names: str) -> None:
    """登记"后端认识的事件类型"（供 snapshot 区分 handled / 未处理）"""
    with _lock:
        for name in type_names:
            if name:
                HANDLED_TYPES.add(str(name))


def _text(value) -> str:
    return "" if value is None else str(value)


def _sid(obj) -> str:
    return _text(getattr(obj, "id", None)) if obj is not None else ""


def satori_summary(event) -> dict:
    """把一条 Satori 事件压成排查需要的那几个 id（不存全量数据）"""
    return {
        "message_id": _sid(getattr(event, "message", None)),
        "user_id": _sid(getattr(event, "user", None)),
        "operator_id": _sid(getattr(event, "operator", None)),
        "emoji_id": _sid(getattr(event, "emoji", None)),
        "channel_id": _sid(getattr(event, "channel", None)),
    }


def record(type_name: str, summary: dict | None = None) -> None:
    """记一笔事件"""
    name = _text(type_name) or "unknown"
    info = summary or {}
    now = time.time()
    with _lock:
        entry = _stats.get(name)
        if entry is None:
            if len(_stats) >= _MAX_TYPES:
                _stats.popitem(last=False)
            entry = {
                "count": 0,
                "first_seen": now,
                "last_seen": now,
                "handled": name in HANDLED_TYPES,
                "last": {},
            }
            _stats[name] = entry
        entry["count"] += 1
        entry["last_seen"] = now
        entry["last"] = info
        entry["handled"] = name in HANDLED_TYPES
        _recent.append({"type": name, "at": now, "handled": entry["handled"], **info})


def record_satori_event(event) -> None:
    record(_text(getattr(event, "type", "")), satori_summary(event))


def snapshot() -> dict:
    """给运维/彩排看的一份快照"""
    now = time.time()
    with _lock:
        types = OrderedDict()
        for name, entry in _stats.items():
            types[name] = {
                "count": entry["count"],
                "handled": entry["handled"],
                "first_seen": entry["first_seen"],
                "last_seen": entry["last_seen"],
                "seconds_since_last": round(now - entry["last_seen"], 1),
                "last": entry["last"],
            }
        return {
            "now": now,
            "handled_types": sorted(HANDLED_TYPES),
            "types": types,
            "recent": list(_recent),
            "unhandled_types": [n for n, e in types.items() if not e["handled"]],
        }


def reset() -> None:
    """清空统计（换一场彩排时用）"""
    with _lock:
        _stats.clear()
        _recent.clear()
