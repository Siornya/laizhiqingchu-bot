from __future__ import annotations

import nonebot
import nonebot_plugin_meme_manager as plugin
from nonebot.adapters.onebot.v11 import Adapter


def test_plugin_loads() -> None:
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    assert plugin.__plugin_meta__.name == "表情包管理"
    assert plugin.sticker is not None
    assert plugin.sticker_upload is not None
