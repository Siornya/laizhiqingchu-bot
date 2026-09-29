from __future__ import annotations

from pydantic import BaseModel


class Config(BaseModel):
    sticker_max_storage_mb: int = 200
    sticker_data_dir: str = ""
    sticker_http_timeout: float = 20.0
    sticker_max_image_mb: int = 20
