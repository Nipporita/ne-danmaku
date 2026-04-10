"""Routes for managing emote aliases (stored in room.db)."""

from __future__ import annotations

from hmac import compare_digest
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from ..danmaku.room_db import RoomDB


class AliasCreate(BaseModel):
    original_name: str
    alias: str


def create_emote_alias_router(emote_alias_token: str) -> APIRouter:
    router = APIRouter()

    def _validate(key: str | None) -> None:
        if not key or not compare_digest(key.strip(), emote_alias_token):
            raise HTTPException(status_code=403, detail="Invalid token")

    @router.get("/emotes")
    async def list_emote_names(request: Request, key: str = Query(None)):
        """Return all scanned emote names (no images)."""
        _validate(key)
        mapping: dict = getattr(request.app.state, "emote_mapping", {})
        return sorted(mapping.keys())

    @router.get("/emote-image/{emote_name}")
    async def get_emote_image_url(request: Request, emote_name: str, key: str = Query(None)):
        """Return the URL path for a single emote (for lazy loading)."""
        _validate(key)
        mapping: dict = getattr(request.app.state, "emote_mapping", {})
        if emote_name not in mapping:
            raise HTTPException(status_code=404, detail="Emote not found")
        return {"url": f"/api/danmaku/v1/emotes/{quote(emote_name, safe='')}"}

    @router.get("/aliases")
    async def list_aliases(request: Request, key: str = Query(None)):
        _validate(key)
        room_db: RoomDB | None = getattr(request.app.state, "room_db", None)
        if room_db is None:
            raise HTTPException(status_code=503, detail="DB not available")
        return room_db.list_emote_aliases()

    @router.post("/aliases")
    async def add_alias(request: Request, body: AliasCreate, key: str = Query(None)):
        _validate(key)
        room_db: RoomDB | None = getattr(request.app.state, "room_db", None)
        if room_db is None:
            raise HTTPException(status_code=503, detail="DB not available")
        mapping: dict = getattr(request.app.state, "emote_mapping", {})
        if body.original_name not in mapping:
            raise HTTPException(status_code=404, detail="Original emote not found")
        alias_id = room_db.add_emote_alias(body.original_name, body.alias.strip())
        return {"id": alias_id, "original_name": body.original_name, "alias": body.alias.strip()}

    @router.delete("/aliases/{alias_id}")
    async def delete_alias(request: Request, alias_id: int, key: str = Query(None)):
        _validate(key)

        room_db: RoomDB = getattr(request.app.state, "room_db", None)
        if room_db is None:
            raise HTTPException(status_code=503, detail="DB not available")
        if not room_db.delete_emote_alias(alias_id):
            raise HTTPException(status_code=404, detail="Alias not found")
        return {"ok": True}

    return router
