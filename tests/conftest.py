from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).parents[2]))


def pytest_configure() -> None:
    os.environ.setdefault("STICKER_DATA_DIR", tempfile.mkdtemp(prefix="sticker-tests-"))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(driver="~fastapi", superusers={"10001"})
