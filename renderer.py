from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from nonebot import logger


def render_menu(text: str, cache_dir: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"menu-{hashlib.sha256(text.encode()).hexdigest()[:16]}.png"
    if target.is_file():
        return target
    font_candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    )
    font_path = next((path for path in font_candidates if Path(path).is_file()), None)
    if font_path is None:
        return None
    try:
        font = ImageFont.truetype(font_path, 32)
        title_font = ImageFont.truetype(font_path, 44)
        lines: list[str] = []
        for line in text.splitlines():
            lines.extend(textwrap.wrap(line, width=34) or [""])
        height = 80 + sum(64 if index == 0 else 48 for index in range(len(lines)))
        image = Image.new("RGB", (1100, max(500, height)), "#fffaf5")
        draw = ImageDraw.Draw(image)
        y = 42
        for index, line in enumerate(lines):
            current_font = title_font if index == 0 else font
            draw.text((55, y), line, font=current_font, fill="#2f2a26")
            y += 64 if index == 0 else 48
        image.save(target, "PNG")
        return target
    except Exception as error:
        logger.warning("表情包菜单渲染失败，将发送文字菜单：{}", error)
        return None
