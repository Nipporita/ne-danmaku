#!/usr/bin/env python3
"""
QQ 官方机器人 → 弹幕姬 桥接器（OneBot v11 反向 WS 客户端）
================================================================

用途
----
把 **QQ 开放平台官方机器人**（QQ Bot OpenAPI v2）推送的群消息，转换成弹幕姬认识的标准
**OneBot v11** 事件，主动连到弹幕姬的 `onebot_v11` 反向 WS 入口，从而让群消息上屏。

    QQ 群
      │  官方 WebSocket 网关（GROUP_MESSAGE_CREATE 全量模式）
      ▼
    本桥接器  ──OneBot v11 反向 WS──▶  弹幕姬（nekocast_danmaku）
                                          │
                                          ▼
                                     OBS 浏览器源（弹幕页）

为什么不用 LLBot / NapCat
------------------------
官方运营规范 §5.4 明确禁止「使用插件、外挂或其他违规第三方工具、服务接入本服务和相关系统」，
第三方协议端正在此列；走官方机器人**合规、无封号风控、也不需要签名服务和 QQ 客户端**。
代价是需要本桥接器（本文件），且只有 OpenID（无真实 QQ 号），所以加了 openid→群号 映射。

依赖
----
    aiohttp, websockets      # 弹幕姬的运行时已经带了这两个包

用法
----
    cp qqbot_bridge.example.json qqbot_bridge.json   # 填 AppID / AppSecret / 映射
    python qqbot_official_bridge.py                  # 正常运行
    python qqbot_official_bridge.py --mock           # 不发网络请求，用内置样例事件自测
    python qqbot_official_bridge.py --mock-file e.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

try:  # websockets >= 14 的新实现
    from websockets.asyncio.client import connect as ws_connect
except Exception:  # pragma: no cover - 兼容旧版
    from websockets import connect as ws_connect  # type: ignore


async def ws_open(url: str, headers: dict[str, str] | None = None, **kw: Any):
    """websockets 新旧版本参数名不同（additional_headers / extra_headers），这里统一。"""
    kw.setdefault("open_timeout", 15)
    if not headers:
        return await ws_connect(url, **kw)
    try:
        return await ws_connect(url, additional_headers=headers, **kw)
    except TypeError:  # websockets < 14
        return await ws_connect(url, extra_headers=headers, **kw)


# ────────────────────────────── 官方协议常量 ──────────────────────────────
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API_BASE_PROD = "https://api.sgroup.qq.com"
API_BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"

INTENT_GROUP_AND_C2C_EVENT = 1 << 25  # 群与单聊事件（含群消息全量、被@、收发开关、进退群等）
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30  # 频道消息（本项目不用，保留常量备查）

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

LOG = logging.getLogger("qqbot-bridge")


# ────────────────────────────── 配置 ──────────────────────────────
@dataclass
class GroupMapping:
    """一个群的映射：官方 OpenID → 弹幕姬用的群号（+ 备注房间名，仅用于日志）"""

    openid: str
    group_id: int
    room: str = ""


@dataclass
class Config:
    app_id: str = ""
    client_secret: str = ""
    sandbox: bool = False
    # 弹幕姬 onebot_v11 反向 WS
    danmaku_url: str = "ws://127.0.0.1:5701/ws"
    danmaku_token: str = ""
    self_id: int = 10000
    # 群映射
    groups: dict[str, GroupMapping] = field(default_factory=dict)
    base_dir: str = ""   # 配置文件所在目录（用于解析相对路径）
    # 过滤
    blacklist_file: str = ""
    max_message_length: int = 50
    forward_images: bool = True
    # 其它
    log_level: str = "INFO"

    @property
    def api_base(self) -> str:
        return API_BASE_SANDBOX if self.sandbox else API_BASE_PROD


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    base_dir = path.parent  # 相对路径（如黑名单）按配置文件所在目录解析
    cfg = Config(
        app_id=str(raw.get("app_id", "")),
        client_secret=str(raw.get("client_secret", "")),
        sandbox=bool(raw.get("sandbox", False)),
        danmaku_url=str(raw.get("danmaku", {}).get("url", "ws://127.0.0.1:5701/ws")),
        danmaku_token=str(raw.get("danmaku", {}).get("access_token", "")),
        self_id=int(raw.get("danmaku", {}).get("self_id", 10000)),
        blacklist_file=str(raw.get("blacklist_file", "")),
        max_message_length=int(raw.get("max_message_length", 50)),
        forward_images=bool(raw.get("forward_images", True)),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        base_dir=str(base_dir),
    )
    for openid, item in (raw.get("groups") or {}).items():
        if isinstance(item, dict):
            gid = item.get("group_id")
            cfg.groups[str(openid)] = GroupMapping(
                openid=str(openid),
                group_id=int(gid) if gid is not None else synthetic_group_id(str(openid)),
                room=str(item.get("room", "")),
            )
        else:  # 简写："openid": 278699690
            cfg.groups[str(openid)] = GroupMapping(str(openid), int(item), "")
    return cfg


def synthetic_group_id(openid: str) -> int:
    """没配真实群号时，用 openid 生成一个稳定的 9 位数字，方便在弹幕姬里做映射。"""
    digest = hashlib.sha1(openid.encode("utf-8")).hexdigest()
    return 100_000_000 + (int(digest[:12], 16) % 900_000_000)


# ────────────────────────────── 内容过滤 ──────────────────────────────
class Filter:
    """复用弹幕姬的 blacklist.txt（每行一条正则），再加长度与 CQ 码转义。"""

    def __init__(self, cfg: Config) -> None:
        self.max_len = cfg.max_message_length
        self.rules: list[re.Pattern[str]] = []
        path = cfg.blacklist_file
        if path:
            p = Path(path)
            if not p.is_absolute():
                base = Path(cfg.base_dir) if cfg.base_dir else Path.cwd()
                p = (base / path).resolve()
            if p.exists():
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        self.rules.append(re.compile(line))
                    except re.error:
                        LOG.debug("跳过无法编译的黑名单规则: %s", line)
                LOG.info("已加载黑名单规则 %d 条（%s）", len(self.rules), p)
            else:
                LOG.warning("黑名单文件不存在，忽略: %s", p)

    def blocked(self, text: str) -> bool:
        return any(r.search(text) for r in self.rules)

    def clean(self, text: str) -> str:
        text = text.replace("\r", " ").replace("\n", " ").strip()
        # 防止文本里的 [CQ:xxx] 被下游当成消息段解析
        text = text.replace("[CQ:", "[CQː")
        if len(text) > self.max_len:
            text = text[: self.max_len]
        return text


# ────────────────────────────── 弹幕姬侧（OneBot v11 反向 WS） ──────────────────────────────
class DanmakuClient:
    """作为 OneBot v11 实现（客户端）连到弹幕姬的 /ws，发送标准事件。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self.connected = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # 弹幕姬用 aiocqhttp（Quart）实现反向 WS，握手要求这几个头：
                #   Authorization: Bearer <token>   —— 与 onebot_v11.access_token 一致
                #   X-Client-Role: Event            —— **必填**，缺失会被直接拒绝（HTTP 400）
                #   X-Self-ID: <机器人号>            —— OneBot 惯例，带上更规范
                headers = {
                    "X-Client-Role": "Event",
                    "X-Self-ID": str(self.cfg.self_id),
                }
                if self.cfg.danmaku_token:
                    headers["Authorization"] = f"Bearer {self.cfg.danmaku_token}"
                async with await ws_open(self.cfg.danmaku_url, headers,
                                        ping_interval=30) as ws:
                    self.connected = True
                    backoff = 1.0
                    LOG.info("已连上弹幕姬：%s", self.cfg.danmaku_url)
                    sender = asyncio.create_task(self._sender(ws))
                    keepalive = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for _ in ws:  # 弹幕姬一般不回推，收到就忽略
                            pass
                    finally:
                        for t in (sender, keepalive):
                            t.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.gather(sender, keepalive, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                LOG.warning("弹幕姬连接断开（%.1fs 后重试）：%s", backoff, exc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)
        self.connected = False

    async def _keepalive(self, ws: Any) -> None:
        """连上后先发 lifecycle.connect，再周期性发 heartbeat（OneBot v11 惯例）。"""
        try:
            await ws.send(json.dumps({
                "post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect",
                "time": int(time.time()), "self_id": self.cfg.self_id,
            }))
            while True:
                await asyncio.sleep(30)
                await ws.send(json.dumps({
                    "post_type": "meta_event", "meta_event_type": "heartbeat",
                    "status": {"online": True, "good": True}, "interval": 30000,
                    "time": int(time.time()), "self_id": self.cfg.self_id,
                }))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.debug("心跳发送失败：%s", exc)

    async def _sender(self, ws: Any) -> None:
        while True:
            event = await self.queue.get()
            await ws.send(json.dumps(event, ensure_ascii=False))

    def submit(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            LOG.warning("弹幕姬发送队列已满，丢弃一条消息")


def to_onebot_event(cfg: Config, mapping: GroupMapping, data: dict[str, Any],
                    filt: Filter, counter: int) -> dict[str, Any] | None:
    """把官方 GROUP_MESSAGE_CREATE 转成 OneBot v11 群消息事件。"""
    author = data.get("author") or {}
    name = str(author.get("username") or "匿名")
    member_openid = str(author.get("member_openid") or author.get("id") or "")
    text = str(data.get("content") or "")

    attachments = data.get("attachments") or []
    image_url = None
    if cfg.forward_images:
        for att in attachments:
            ctype = str(att.get("content_type") or "")
            if ctype.startswith("image/") and att.get("url"):
                image_url = str(att["url"])
                break

    # 组消息段：有文字就发文字；纯图片发图片段（弹幕姬会当表情处理）
    if text.strip():
        # 以 / 开头的是指令（/sc、/gift 等），绕开普通弹幕黑名单，避免误伤
        is_command = text.lstrip().startswith("/")
        if not is_command and filt.blocked(text):
            LOG.info("命中黑名单，已拦截：[%s] %s: %s", mapping.room or mapping.group_id, name, text[:40])
            return None
        clean = filt.clean(text)
        if not clean:
            return None
        message = [{"type": "text", "data": {"text": clean}}]
        raw = clean
    elif image_url:
        message = [{"type": "image", "data": {"url": image_url}}]
        raw = "[图片]"
    else:
        return None  # 空消息（表情、红包等未支持类型）

    ts = data.get("timestamp")
    try:
        unix = int(time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))) if ts else int(time.time())
    except Exception:  # noqa: BLE001
        unix = int(time.time())

    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": counter,
        "group_id": mapping.group_id,
        "user_id": member_openid,
        "message": message,
        "raw_message": raw,
        "font": 0,
        "sender": {
            "user_id": member_openid,
            "nickname": name,
            "card": "",
            "role": str(author.get("member_role") or "member"),
        },
        "time": unix,
        "self_id": cfg.self_id,
    }


# ────────────────────────────── 官方网关客户端 ──────────────────────────────
class QQGovClient:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession, danmaku: DanmakuClient,
                 filt: Filter) -> None:
        self.cfg = cfg
        self.session = session
        self.danmaku = danmaku
        self.filt = filt
        self.token = ""
        self.token_expire_at = 0.0
        self.session_id = ""
        self.last_seq: int | None = None
        self.counter = 0
        self._stop = asyncio.Event()
        self._unknown_groups: set[str] = set()

    # ---- access_token ----
    async def ensure_token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self.token and now < self.token_expire_at - 120:
            return self.token
        async with self.session.post(
            TOKEN_URL, json={"appId": self.cfg.app_id, "clientSecret": self.cfg.client_secret},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.json(content_type=None)
        # 官方约定：失败时 HTTP 仍是 200，必须看 code
        if not isinstance(body, dict) or "access_token" not in body:
            code = body.get("code") if isinstance(body, dict) else "?"
            raise RuntimeError(f"获取 access_token 失败（code={code}）: {body}")
        self.token = str(body["access_token"])
        expires = int(body.get("expires_in") or 7200)
        self.token_expire_at = now + expires
        LOG.info("已获取 access_token（%d 秒后过期）", expires)
        return self.token

    async def gateway_url(self) -> str:
        token = await self.ensure_token()
        async with self.session.get(
            f"{self.cfg.api_base}/websocket/",
            headers={"Authorization": f"QQBot {token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.json(content_type=None)
        url = (body or {}).get("url")
        if not url:
            raise RuntimeError(f"获取网关地址失败: {body}")
        return str(url)

    # ---- 主循环 ----
    async def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                url = await self.gateway_url()
                async with ws_connect(url, open_timeout=15, ping_interval=None) as ws:
                    backoff = 2.0
                    await self._session_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("网关连接异常（%.1fs 后重试）：%s", backoff, exc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60.0)

    async def _session_loop(self, ws: Any) -> None:
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == OP_HELLO:
                    interval = float((msg.get("d") or {}).get("heartbeat_interval", 40000)) / 1000.0
                    await self._identify_or_resume(ws)
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws, interval))
                elif op == OP_DISPATCH:
                    self.last_seq = msg.get("s", self.last_seq)
                    await self._dispatch(str(msg.get("t") or ""), msg.get("d") or {})
                elif op == OP_HEARTBEAT_ACK:
                    LOG.debug("心跳已确认")
                elif op == OP_RECONNECT:
                    LOG.warning("官方要求重连")
                    break
                elif op == OP_INVALID_SESSION:
                    LOG.warning("会话失效（op=9），将重新鉴权")
                    self.session_id = ""
                    self.last_seq = None
                    break
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _identify_or_resume(self, ws: Any) -> None:
        token = await self.ensure_token()
        if self.session_id and self.last_seq is not None:
            await ws.send(json.dumps({
                "op": OP_RESUME,
                "d": {"token": f"QQBot {token}", "session_id": self.session_id, "seq": self.last_seq},
            }))
            LOG.info("已发送 Resume（session=%s seq=%s）", self.session_id, self.last_seq)
        else:
            await ws.send(json.dumps({
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENT_GROUP_AND_C2C_EVENT,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$sdk": "nekocast-bridge", "$device": "server"},
                },
            }))
            LOG.info("已发送 Identify（intents=%d）", INTENT_GROUP_AND_C2C_EVENT)

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.last_seq}))

    # ---- 事件分发 ----
    async def _dispatch(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "READY":
            user = (data.get("user") or {}).get("username")
            self.session_id = str(data.get("session_id") or "")
            LOG.info("机器人已就绪：%s（session=%s）", user or "?", self.session_id)
            return

        if event_type == "RESUMED":
            LOG.info("会话已恢复")
            return

        if event_type == "GROUP_MESSAGE_CREATE":
            await self._on_group_message(data)
            return

        if event_type == "GROUP_MSG_RECEIVE":
            openid = str(data.get("group_openid") or "")
            LOG.info("★ 群 %s 已开启「接收所有消息」（全量模式），群消息会上屏：%s",
                     openid, self._describe(openid))
            return

        if event_type == "GROUP_MSG_REJECT":
            openid = str(data.get("group_openid") or "")
            LOG.warning("★ 群 %s 关闭了「接收所有消息」，该群不再上屏：%s",
                        openid, self._describe(openid))
            return

        if event_type in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT",
                          "GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"):
            LOG.info("群事件 %s：group=%s", event_type, data.get("group_openid"))
            return

        LOG.debug("未处理事件 %s", event_type)

    async def _on_group_message(self, data: dict[str, Any]) -> None:
        openid = str(data.get("group_openid") or "")
        mapping = self.cfg.groups.get(openid)
        if mapping is None:
            if openid not in self._unknown_groups:
                self._unknown_groups.add(openid)
                LOG.warning(
                    "收到未配置群的消息（group_openid=%s）。请把它填进 qqbot_bridge.json 的 groups，"
                    "例如：\"%s\": {\"group_id\": <你的真实群号>, \"room\": \"弹幕群\"}；"
                    "若不想填真实群号，可不写 group_id，程序会使用映射号 %d（需同步加到弹幕姬 config.json 的 "
                    "onebot_v11.group_map 里）",
                    openid, openid, synthetic_group_id(openid),
                )
            return

        self.counter += 1
        event = to_onebot_event(self.cfg, mapping, data, self.filt, self.counter)
        if event is None:
            return
        self.danmaku.submit(event)
        LOG.info("[%s] %s: %s", mapping.room or mapping.group_id,
                 (data.get("author") or {}).get("username"), (data.get("content") or "[图片]")[:40])

    def _describe(self, openid: str) -> str:
        m = self.cfg.groups.get(openid)
        if not m:
            return "未配置映射"
        return f"群号 {m.group_id}" + (f"，房间「{m.room}」" if m.room else "")

    def stop(self) -> None:
        self._stop.set()


# ────────────────────────────── 自测：本地灌入样例事件 ──────────────────────────────
MOCK_EVENTS: list[dict[str, Any]] = [
    {
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "ROBOT1.0_mock_1", "content": "这是一条自测弹幕",
            "group_openid": "MOCK_GROUP_OPENID",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "author": {"username": "自测用户A", "member_openid": "MOCK_USER_A", "member_role": "member"},
        },
    },
    {
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "ROBOT1.0_mock_2", "content": "/sc 10 自测超级聊天",
            "group_openid": "MOCK_GROUP_OPENID",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "author": {"username": "自测用户B", "member_openid": "MOCK_USER_B", "member_role": "admin"},
        },
    },
    {
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "ROBOT1.0_mock_3", "content": "",
            "group_openid": "MOCK_GROUP_OPENID",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "author": {"username": "自测用户C", "member_openid": "MOCK_USER_C", "member_role": "member"},
            "attachments": [{"content_type": "image/png", "url": "https://example.invalid/mock.png"}],
        },
    },
    {"t": "GROUP_MSG_RECEIVE", "d": {"group_openid": "MOCK_GROUP_OPENID"}},
]


async def run_mock(cfg: Config, danmaku: DanmakuClient, filt: Filter, mock_file: str | None) -> None:
    events = MOCK_EVENTS
    if mock_file:
        loaded = json.loads(Path(mock_file).read_text(encoding="utf-8"))
        events = loaded if isinstance(loaded, list) else [loaded]
    LOG.info("自测模式：注入 %d 条样例事件（不连接官方网关）", len(events))
    fake = QQGovClient(cfg, session=None, danmaku=danmaku, filt=filt)  # type: ignore[arg-type]
    await asyncio.sleep(2)  # 等弹幕姬连接建立
    for item in events:
        await fake._dispatch(str(item.get("t")), item.get("d") or {})  # noqa: SLF001
        await asyncio.sleep(0.5)
    LOG.info("自测事件已发送完毕，等待 5 秒让弹幕姬处理…")
    await asyncio.sleep(5)


# ────────────────────────────── 入口 ──────────────────────────────
async def amain(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        print(f"找不到配置文件：{cfg_path}\n请先复制 qqbot_bridge.example.json 为 qqbot_bridge.json 并填写。",
              file=sys.stderr)
        return 2
    cfg = load_config(cfg_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    if not args.mock and (not cfg.app_id or not cfg.client_secret):
        print("配置里缺少 app_id / client_secret（也可用 --mock 自测）。", file=sys.stderr)
        return 2

    filt = Filter(cfg)
    danmaku = DanmakuClient(cfg)
    tasks = [asyncio.create_task(danmaku.run(), name="danmaku")]
    stop_event = asyncio.Event()

    if args.mock:
        tasks.append(asyncio.create_task(run_mock(cfg, danmaku, filt, args.mock_file), name="mock"))
    else:
        async with aiohttp.ClientSession() as session:
            gov = QQGovClient(cfg, session, danmaku, filt)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop_event.set)
            tasks.append(asyncio.create_task(gov.run(), name="qqgov"))
            LOG.info("桥接器已启动：官方网关 %s → 弹幕姬 %s", cfg.api_base, cfg.danmaku_url)
            await stop_event.wait()
            gov.stop()

    # mock 模式：等 mock 任务收尾
    if args.mock:
        await asyncio.wait([tasks[-1]], timeout=30)

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    LOG.info("已退出")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ 官方机器人 → 弹幕姬 桥接器")
    parser.add_argument("--config", default=str(Path(__file__).with_name("qqbot_bridge.json")),
                        help="配置文件名（默认同目录 qqbot_bridge.json）")
    parser.add_argument("--mock", action="store_true", help="注入内置样例事件自测，不连官方网关")
    parser.add_argument("--mock-file", default=None, help="用指定 JSON 文件里的事件做自测")
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
