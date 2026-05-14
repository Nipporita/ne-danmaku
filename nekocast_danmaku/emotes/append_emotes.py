from pathlib import Path
import os

from loguru import logger

from satori.element import Element, Text, Image as ImageElement

import re
from pathlib import Path

import httpx

from PIL import Image

INVALID_FILENAME_CHARS = r'[<>:"/\\|?*\n\r\t]'

# 只提取【...】中的...作为文件名
PATTERN_BRACKETS = re.compile(r"【([^】]+)】")


def sanitize_filename(name: str) -> str:
    name = re.sub(INVALID_FILENAME_CHARS, "_", name)
    name = name.strip().strip(".")
    return name or "emote"


async def append_emotes(asset_dir: Path, elements: list[Element]) -> None:
    all_text = "".join(
        i.text for i in elements if isinstance(i, Text)
    ).strip()
    
    m = PATTERN_BRACKETS.search(all_text)
    if m:
        all_text = m.group(1)
    
    if not all_text:
        return

    img = next(
        (i for i in elements if isinstance(i, ImageElement)),
        None,
    )

    if img is None:
        return

    emote_name = sanitize_filename(all_text)

    emotes_root = asset_dir / "touhou"
    emotes_root.mkdir(parents=True, exist_ok=True)

    emote_url = img.src

    logger.debug(
        "Attempting to download emote '{}' from {}",
        emote_name,
        emote_url,
    )

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(emote_url, timeout=20)

        if r.status_code != 200:
            logger.warning(
                "Failed to download emote {}: HTTP {}",
                emote_url,
                r.status_code,
            )
            return

        # ============================================================
        # detect image format
        # ============================================================

        try:
            from io import BytesIO

            image = Image.open(BytesIO(r.content))
            ext = image.format.lower()

        except Exception as e:
            logger.warning(
                "Failed to detect image format for {}: {}",
                emote_url,
                e,
            )
            return

        if ext not in {"png", "gif", "webp", "jpeg", "jpg"}:
            logger.warning(
                "Unsupported emote format '{}' from {}",
                ext,
                emote_url,
            )
            return

        # ============================================================
        # resolve duplicate filename
        # ============================================================

        target_full_path = emotes_root / f"{emote_name}.{ext}"

        c = 1
        while target_full_path.exists():
            logger.warning(
                "Emote file {} already exists",
                target_full_path,
            )

            target_full_path = (
                emotes_root / f"{emote_name}_{c}.{ext}"
            )

            c += 1

        # ============================================================
        # save
        # ============================================================

        target_full_path.write_bytes(r.content)

        logger.info(
            "Saved new emote '{}' -> {}",
            emote_name,
            target_full_path,
        )

    except Exception as e:
        logger.exception(
            "Error downloading emote from {}: {}",
            emote_url,
            e,
        )