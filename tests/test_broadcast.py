"""ConnectionManager.broadcast_config fans out to every connected group."""

from __future__ import annotations

import asyncio
import json

from nekocast_danmaku.danmaku.models import ConnectionManager


class _FakeWebSocket:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("socket is closed")
        self.sent.append(payload)


def test_broadcast_config_reaches_every_group():
    async def scenario():
        cm = ConnectionManager()
        first, second = _FakeWebSocket(), _FakeWebSocket()
        cm.client_connections["g1"].add(first)
        cm.client_connections["g2"].add(second)

        payload = {
            "type": "config",
            "config_version": 3,
            "max_message_length": 12,
        }
        await cm.broadcast_config(payload)
        return first, second

    first, second = asyncio.run(scenario())

    for ws in (first, second):
        assert json.loads(ws.sent[0]) == {
            "type": "config",
            "config_version": 3,
            "max_message_length": 12,
        }


def test_broadcast_config_prunes_dead_connections():
    async def scenario():
        cm = ConnectionManager()
        alive, dead = _FakeWebSocket(), _FakeWebSocket(fail=True)
        cm.client_connections["g1"].add(alive)
        cm.client_connections["g1"].add(dead)

        await cm.broadcast_config({"type": "config"})
        return cm, alive, dead

    cm, alive, dead = asyncio.run(scenario())

    assert alive in cm.client_connections["g1"]
    assert dead not in cm.client_connections["g1"]


def test_broadcast_config_with_no_clients_is_a_noop():
    cm = ConnectionManager()
    asyncio.run(cm.broadcast_config({"type": "config"}))
