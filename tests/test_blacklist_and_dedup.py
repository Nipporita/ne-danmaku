"""Blacklist / dedup tag messages as ``blocked`` instead of dropping them."""

from __future__ import annotations

from nekocast_danmaku.danmaku.danmaku_class.danmaku_message import (
    PlainDanmakuMessage,
    SuperChatMessage,
)
from nekocast_danmaku.danmaku.models import BlacklistService, DanmakuFilter


def _plain(text: str, sender: str = "tester", sender_id: str = "uid-1"):
    return PlainDanmakuMessage(text=text, sender=sender, senderId=sender_id)


def _superchat(text: str, sender: str = "tester"):
    return SuperChatMessage(
        text=text, sender=sender, senderId="uid-1", duration=10, cost=30.0
    )


# --------------------------------------------------------------------------
# BlacklistService.check_message
# --------------------------------------------------------------------------

def test_pattern_marks_blocked_instead_of_filtering():
    bl = BlacklistService()
    bl.append_pattern("买号")

    msg = _plain("低价买号")
    bl.check_message(msg)

    assert msg.blocked is True


def test_clean_message_is_not_blocked():
    bl = BlacklistService()
    bl.append_pattern("买号")

    msg = _plain("今天天气不错")
    bl.check_message(msg)

    assert msg.blocked is False


def test_plain_message_keeps_full_text_when_blocked():
    """Plain danmaku keep their text so game commands still see it verbatim."""
    bl = BlacklistService()
    bl.append_pattern("买号")

    msg = _plain("买号")
    bl.check_message(msg)

    assert msg.blocked is True
    assert msg.text == "买号"


def test_superchat_text_is_masked():
    """SC renders on screen, so the matched span is censored in place."""
    bl = BlacklistService()
    bl.append_pattern("买号")

    msg = _superchat("快来买号呀")
    bl.check_message(msg)

    assert "买号" not in msg.text
    assert msg.text == "快来**呀"


def test_forbidden_user_marks_blocked():
    bl = BlacklistService()
    bl._forbidden_users = {"uid-7"}

    msg = _plain("hi", sender_id="uid-7")
    bl.check_message(msg)

    assert msg.blocked is True


# --------------------------------------------------------------------------
# DanmakuFilter dedup
# --------------------------------------------------------------------------

def test_dedup_marks_repeat_but_first_pass_is_clean():
    f = DanmakuFilter(blacklist=None, dedup_window=60)

    first = _plain("same")
    f.check_message("g", first)
    assert first.blocked is False

    second = _plain("same")
    f.check_message("g", second)
    assert second.blocked is True


def test_dedup_does_not_consume_slot_for_duplicates():
    f = DanmakuFilter(blacklist=None, dedup_window=60)

    f.check_message("g", _plain("same"))
    f.check_message("g", _plain("same"))
    f.check_message("g", _plain("same"))

    assert len(f.recent_messages["g"]) == 1


def test_dedup_disabled_when_window_not_positive():
    """The real config.json ships dedup_window = -1 to turn dedup off."""
    f = DanmakuFilter(blacklist=None, dedup_window=-1)

    f.check_message("g", _plain("same"))
    second = _plain("same")
    f.check_message("g", second)

    assert second.blocked is False


def test_dedup_is_scoped_per_group():
    f = DanmakuFilter(blacklist=None, dedup_window=60)

    f.check_message("g1", _plain("same"))
    other = _plain("same")
    f.check_message("g2", other)

    assert other.blocked is False


def test_blacklist_runs_through_danmaku_filter():
    bl = BlacklistService()
    bl.append_pattern("买号")
    f = DanmakuFilter(blacklist=bl, dedup_window=60)

    msg = _plain("快来买号")
    f.check_message("g", msg)

    assert msg.blocked is True
