"""Resolve builtin emote names (and aliases) from plain text ``[name]``."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from pathlib import Path
from urllib.parse import quote

if TYPE_CHECKING:
    from ..danmaku.room_db import RoomDB

EMOTE_PATTERN = re.compile(
    r"[\[【](.+?)[\]】](?:\s*\*\s*(\d+))?"
)

from loguru import logger

class EmoteResolver:
    """Converts ``[emote_name]`` plain messages into emote URLs.

    Reads the scanned emote mapping and DB aliases to support real-time
    alias resolution without frontend involvement.
    """

    def __init__(self, emote_mapping: dict[str, Path], room_db: RoomDB | None = None):
        self._emote_mapping = emote_mapping
        self._room_db = room_db

    def resolve(self, text: str) -> list[str] | None:
        """Return a list of emote URLs if *text* matches ``[name]`` and the name
        (or an alias) is a known emote.  Otherwise return an empty list.
        """
        result = []
        
        url, times, remaining = self._resolve(text)
        while url:
            result.extend([url] * times)
            url, times, remaining = self._resolve(remaining)
        
        if not result:
            return None
        
        return result
    
    def _resolve(self, text: str) -> tuple[str | None, int, str]:
        """Return an emote URL if *text* matches ``[name]`` and the name
        (or an alias) is a known emote.  Otherwise return ``None``.
        """
        m = EMOTE_PATTERN.match(text.strip())
        if not m:
            return None, 0, m
        name = m.group(1)
        times = m.group(2) or "1"
        # 剩余字符串
        remaining = text[m.end():].strip()
        
        logger.debug("Resolved emote '{}' with times {} and remaining '{}'", name, times, remaining)

        # direct match
        if name in self._emote_mapping:
            return f"/api/danmaku/v1/emotes/{quote(name, safe='')}", int(times), remaining

        # alias lookup (scan DB each time so changes are real-time)
        if self._room_db:
            for alias_row in self._room_db.list_emote_aliases():
                if alias_row["alias"] == name:
                    original = alias_row["original_name"]
                    if original in self._emote_mapping:
                        return f"/api/danmaku/v1/emotes/{quote(original, safe='')}", int(times), remaining

        return None, 0, m
    
    
