from __future__ import annotations

import asyncio
import re
import secrets
import time
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, on_message, on_notice, require
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .config import Config
from .media import (
    download_file,
    file_segments,
    forward_images,
    image_content,
    image_segments,
    is_forward_message,
    message_file_source,
)
from .renderer import render_menu
from .storage import StickerStore, bytes_md5, valid_keyword

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as localstore  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="表情包管理",
    description="按关键词收藏、随机发送、查找和管理群聊表情包",
    usage="发送“表情包管理菜单”查看指令",
    type="application",
    homepage="https://github.com/Siornya/laizhiqingchu-bot",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

plugin_config = get_plugin_config(Config)
data_root = (
    Path(plugin_config.sticker_data_dir)
    if plugin_config.sticker_data_dir
    else localstore.get_data_dir("nonebot_plugin_meme_manager") / "stickers"
)
store = StickerStore(
    data_root,
    default_limit_mb=plugin_config.sticker_max_storage_mb,
)

CONFIRM_TTL = 60
FORWARD_TTL = 90
RESTORE_TTL = 120
MAX_BACKUP_BYTES = 512 * 1024 * 1024
PAGE_SIZE = 10
PREFIXES = (
    "批量添加合并转发",
    "表情包管理菜单",
    "创建备份",
    "恢复备份",
    "清空图库",
    "屏蔽列表",
    "解除屏蔽",
    "添加",
    "来只",
    "列表",
    "删图",
    "删除",
    "统计",
    "屏蔽",
)
MENU_TEXT = """表情包管理菜单
所有人可用
• 添加{关键词}：回复图片或随命令发送图片，自动去重入库
• 批量添加合并转发{关键词}：90 秒内发送合并转发批量入库
• 来只{关键词}：随机发送一张图片

管理员可用
• 列表{关键词}{页码}：分页查看图片文件名
• 删图：回复图片，按内容精准删除
• 删图{关键词}{序号}：按列表序号删除
• 删除{关键词}：60 秒内二次发送确认删除分类
• 统计：查看关键词数、图片数和 Top 3
• 屏蔽{关键词} / 解除屏蔽{关键词} / 屏蔽列表
• 创建备份：生成 zip 并作为聊天文件发送
• 恢复备份：120 秒内上传备份 zip，合并恢复图库
• 清空图库：60 秒内二次发送确认清空

管理员仅使用 NoneBot SUPERUSERS。"""

pending_delete: dict[str, tuple[str, float]] = {}
pending_forward: dict[str, tuple[str, float]] = {}
pending_restore: dict[str, float] = {}
pending_clear: dict[str, float] = {}


def _text(event: MessageEvent) -> str:
    text = event.get_plaintext().strip()
    if text.startswith("/"):
        text = text[1:].lstrip()
    return text


def _scope(event: MessageEvent) -> str:
    location = (
        f"group:{event.group_id}" if isinstance(event, GroupMessageEvent) else "private"
    )
    return f"{event.self_id}:{location}:{event.user_id}"


def _group_scope(event: GroupUploadNoticeEvent) -> str:
    return f"{event.self_id}:group:{event.group_id}:{event.user_id}"


def _is_admin(event: MessageEvent | GroupUploadNoticeEvent) -> bool:
    user_id = str(event.user_id)
    superusers = {str(item) for item in get_driver().config.superusers}
    return user_id in superusers


async def _sticker_rule(event: MessageEvent) -> bool:
    text = _text(event)
    if any(text.startswith(prefix) for prefix in PREFIXES):
        return True
    if is_forward_message(event.message):
        state = pending_forward.get(_scope(event))
        return bool(state and time.time() - state[1] <= FORWARD_TTL)
    if file_segments(event):
        key = _scope(event)
        started = pending_restore.get(key, 0)
        if started and time.time() - started <= RESTORE_TTL:
            return True
        pending_restore.pop(key, None)
    return False


async def _group_upload_rule(event: Event) -> bool:
    return isinstance(event, GroupUploadNoticeEvent)


sticker = on_message(Rule(_sticker_rule), priority=10, block=True)
sticker_upload = on_notice(Rule(_group_upload_rule), priority=10, block=False)


async def _load_images(
    bot: Bot, segments: list[MessageSegment]
) -> tuple[list[tuple[bytes, str]], int]:
    loaded: list[tuple[bytes, str]] = []
    failed = 0
    for segment in segments:
        try:
            loaded.append(
                await image_content(
                    bot,
                    segment,
                    timeout=plugin_config.sticker_http_timeout,
                    max_bytes=plugin_config.sticker_max_image_mb * 1024 * 1024,
                )
            )
        except Exception as error:
            failed += 1
            logger.warning("读取表情图片失败：{}", error)
    return loaded, failed


def _keyword_error(keyword: str) -> str | None:
    if not keyword:
        return "缺少关键词"
    if not valid_keyword(keyword):
        return "关键词不合法：不能含空格、路径字符、Windows 保留名或尾随点号"
    return None


async def _handle_add(bot: Bot, event: MessageEvent, matcher: Matcher, text: str) -> None:
    keyword = text[len("添加") :].strip()
    if error := _keyword_error(keyword):
        await matcher.finish(error)
    if keyword in store.blocked_keywords:
        await matcher.finish(f"关键词 {keyword} 已被屏蔽")
    segments = image_segments(event)
    if not segments:
        await matcher.finish("未取得图片，请回复一条图片消息，或把图片和命令一起发送")
    images, failed = await _load_images(bot, segments)
    added, skipped = await asyncio.to_thread(store.add_images, keyword, images)
    await matcher.finish(
        f"成功添加 {added} 张，跳过 {skipped} 张重复"
        + (f"，{failed} 张读取失败" if failed else "")
    )


async def _handle_forward(
    bot: Bot, event: MessageEvent, matcher: Matcher, text: str
) -> None:
    if text.startswith("批量添加合并转发"):
        keyword = text[len("批量添加合并转发") :].strip()
        if error := _keyword_error(keyword):
            await matcher.finish(error)
        if keyword in store.blocked_keywords:
            await matcher.finish(f"关键词 {keyword} 已被屏蔽")
        pending_forward[_scope(event)] = (keyword, time.time())
        await matcher.finish(f"请在 {FORWARD_TTL} 秒内发送合并转发")
    state = pending_forward.pop(_scope(event), None)
    if state is None or time.time() - state[1] > FORWARD_TTL:
        return
    segments = await forward_images(bot, event.message)
    if not segments:
        await matcher.finish("合并转发中没有读取到图片")
    images, failed = await _load_images(bot, segments)
    added, skipped = await asyncio.to_thread(store.add_images, state[0], images)
    await matcher.finish(
        f"批量添加完成：新增 {added} 张，跳过 {skipped} 张重复"
        + (f"，{failed} 张读取失败" if failed else "")
    )


async def _handle_send(matcher: Matcher, text: str) -> None:
    keyword = text[len("来只") :].strip()
    if error := _keyword_error(keyword):
        await matcher.finish(error)
    if keyword in store.blocked_keywords:
        await matcher.finish(f"关键词 {keyword} 已被屏蔽")
    files = store.prune(keyword)
    if not files:
        await matcher.finish(f"关键词 {keyword} 下还没有图片")
    path = store.data_dir / keyword / secrets.choice(files)
    await matcher.finish(MessageSegment.image(path.read_bytes()))


def _parse_list(raw: str) -> tuple[str, int]:
    match = re.search(r"(\d+)\s*$", raw)
    if not match:
        return raw.strip(), 1
    keyword = raw[: match.start()].strip()
    return (keyword, int(match.group(1))) if keyword else (raw.strip(), 1)


async def _handle_list(event: MessageEvent, matcher: Matcher, text: str) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    keyword, page = _parse_list(text[len("列表") :].strip())
    if error := _keyword_error(keyword):
        await matcher.finish(error)
    if keyword in store.blocked_keywords:
        await matcher.finish(f"关键词 {keyword} 已被屏蔽")
    files = store.prune(keyword)
    if not files:
        await matcher.finish("该关键词下没有图片")
    pages = (len(files) + PAGE_SIZE - 1) // PAGE_SIZE
    if page < 1 or page > pages:
        await matcher.finish(f"页码超出范围，共 {pages} 页")
    start = (page - 1) * PAGE_SIZE
    lines = [
        f"[{index}] {name}"
        for index, name in enumerate(files[start : start + PAGE_SIZE], start + 1)
    ]
    lines.append(f"第 {page}/{pages} 页，共 {len(files)} 张")
    await matcher.finish("\n".join(lines))


async def _handle_delete_image(
    bot: Bot, event: MessageEvent, matcher: Matcher, text: str
) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    segments = image_segments(event)
    rest = text[len("删图") :].strip()
    if segments:
        if rest and not valid_keyword(rest):
            await matcher.finish("关键词不合法")
        images, failed = await _load_images(bot, segments)
        hashes = {bytes_md5(content) for content, _ in images}
        deleted = await asyncio.to_thread(store.delete_by_hashes, hashes, rest or None)
        count = sum(deleted.values())
        if not count:
            await matcher.finish("没有找到与回复图片相同的表情")
        summary = "、".join(f"{key} {value} 张" for key, value in deleted.items())
        await matcher.finish(
            f"已删除 {count} 张（{summary}）"
            + (f"，{failed} 张回复图片读取失败" if failed else "")
        )
    match = re.search(r"(\d+)\s*$", rest)
    if not match:
        await matcher.finish("用法：回复图片发送“删图”，或发送“删图{关键词}{序号}”")
    keyword = rest[: match.start()].strip()
    if error := _keyword_error(keyword):
        await matcher.finish(error)
    index = int(match.group(1))
    try:
        filename = await asyncio.to_thread(store.delete_index, keyword, index)
    except IndexError:
        await matcher.finish("序号无效")
    await matcher.finish(f"已删除：{filename}")


async def _handle_delete_keyword(event: MessageEvent, matcher: Matcher, text: str) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    keyword = text[len("删除") :].strip()
    if error := _keyword_error(keyword):
        await matcher.finish(error)
    key = _scope(event)
    now = time.time()
    state = pending_delete.get(key)
    if state and state[0] == keyword and now - state[1] <= CONFIRM_TTL:
        pending_delete.pop(key, None)
        count = await asyncio.to_thread(store.delete_keyword, keyword)
        await matcher.finish(f"已删除关键词 {keyword} 的 {count} 张图片")
    pending_delete[key] = (keyword, now)
    await matcher.finish(f"请在 {CONFIRM_TTL} 秒内再次发送“删除{keyword}”确认")


async def _handle_admin(event: MessageEvent, matcher: Matcher, text: str) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    if text.startswith("统计"):
        keywords, images, top = store.stats()
        used_mb = store.storage_bytes() / 1024 / 1024
        lines = [
            f"关键词数：{keywords}",
            f"图片总数：{images}",
            f"存储占用：{used_mb:.1f} / {store.max_storage_mb} MB",
            "图片数 Top 3：",
        ]
        lines.extend(f"{keyword}: {count} 张" for keyword, count in top)
        await matcher.finish("\n".join(lines))
    if text.startswith("屏蔽列表"):
        content = "\n".join(sorted(store.blocked_keywords)) or "暂无屏蔽关键词"
        await matcher.finish("屏蔽关键词列表：\n" + content)
    if text.startswith("解除屏蔽"):
        keyword = text[len("解除屏蔽") :].strip()
        if error := _keyword_error(keyword):
            await matcher.finish(error)
        store.blocked_keywords.discard(keyword)
        await asyncio.to_thread(store.save_blocked)
        await matcher.finish(f"已解除屏蔽：{keyword}")
    keyword = text[len("屏蔽") :].strip()
    if error := _keyword_error(keyword):
        await matcher.finish(error)
    store.blocked_keywords.add(keyword)
    await asyncio.to_thread(store.save_blocked)
    await matcher.finish(f"已屏蔽：{keyword}")


async def _restore_source(source: str, name: str) -> str:
    if Path(name).suffix.lower() != ".zip":
        raise ValueError("恢复文件必须是 zip 格式")
    cache_dir = localstore.get_cache_dir("nonebot_plugin_meme_manager")
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_dir / f"restore_{secrets.token_hex(8)}.zip"
    try:
        await download_file(
            source,
            temporary,
            timeout=max(plugin_config.sticker_http_timeout, 120),
            max_bytes=MAX_BACKUP_BYTES,
        )
        result = await asyncio.to_thread(store.restore, temporary)
        return str(result["message"])
    finally:
        temporary.unlink(missing_ok=True)


async def _handle_create_backup(bot: Bot, event: MessageEvent, matcher: Matcher) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    info = await asyncio.to_thread(store.create_backup)
    path = store.backup_path(str(info["filename"]))
    if path is None:
        await matcher.finish("备份已生成，但无法读取备份文件")
    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "upload_group_file",
                group_id=event.group_id,
                file=str(path),
                name=path.name,
            )
        else:
            await bot.call_api(
                "upload_private_file",
                user_id=event.user_id,
                file=str(path),
                name=path.name,
            )
    except Exception as error:
        logger.exception("发送表情包备份失败")
        await matcher.finish(f"备份已保存在服务器，但发送失败：{error}")
    path.unlink(missing_ok=True)
    size_mb = int(info["size"]) / 1024 / 1024
    await matcher.finish(
        f"备份完成：{info['keywords']} 个关键词、{info['images']} 张图片，"
        f"文件大小 {size_mb:.1f} MB"
    )


async def _handle_restore(bot: Bot, event: MessageEvent, matcher: Matcher) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    segments = file_segments(event)
    if not segments:
        pending_restore[_scope(event)] = time.time()
        await matcher.finish(f"请在 {RESTORE_TTL} 秒内上传备份 zip 文件")
    try:
        source, name = await message_file_source(bot, segments[0])
        message = await _restore_source(source, name)
    except Exception as error:
        await matcher.finish(f"恢复失败：{error}")
    pending_restore.pop(_scope(event), None)
    await matcher.finish(message)


async def _handle_clear(event: MessageEvent, matcher: Matcher) -> None:
    if not _is_admin(event):
        await matcher.finish("此指令仅限管理员")
    key = _scope(event)
    now = time.time()
    started = pending_clear.get(key, 0)
    if started and now - started <= CONFIRM_TTL:
        pending_clear.pop(key, None)
        keywords, images = await asyncio.to_thread(store.clear)
        await matcher.finish(f"已清空 {keywords} 个关键词、{images} 张图片")
    pending_clear[key] = now
    await matcher.finish(f"请在 {CONFIRM_TTL} 秒内再次发送“清空图库”确认")


@sticker.handle()
async def handle_sticker(bot: Bot, event: MessageEvent, matcher: Matcher) -> None:
    text = _text(event)
    if file_segments(event) and _scope(event) in pending_restore:
        await _handle_restore(bot, event, matcher)
        return
    if is_forward_message(event.message):
        await _handle_forward(bot, event, matcher, text)
        return
    if text.startswith("批量添加合并转发"):
        await _handle_forward(bot, event, matcher, text)
    elif text.startswith("添加"):
        await _handle_add(bot, event, matcher, text)
    elif text.startswith("来只"):
        await _handle_send(matcher, text)
    elif text.startswith("列表"):
        await _handle_list(event, matcher, text)
    elif text.startswith("删图"):
        await _handle_delete_image(bot, event, matcher, text)
    elif text.startswith("删除"):
        await _handle_delete_keyword(event, matcher, text)
    elif text.startswith("创建备份"):
        await _handle_create_backup(bot, event, matcher)
    elif text.startswith("恢复备份"):
        await _handle_restore(bot, event, matcher)
    elif text.startswith("清空图库"):
        await _handle_clear(event, matcher)
    elif text.startswith(("统计", "屏蔽列表", "解除屏蔽", "屏蔽")):
        await _handle_admin(event, matcher, text)
    elif text.startswith("表情包管理菜单"):
        image = await asyncio.to_thread(
            render_menu,
            MENU_TEXT,
            localstore.get_cache_dir("nonebot_plugin_meme_manager"),
        )
        await matcher.finish(
            MessageSegment.image(image.read_bytes()) if image else MENU_TEXT
        )


@sticker_upload.handle()
async def handle_group_upload(bot: Bot, event: GroupUploadNoticeEvent) -> None:
    key = _group_scope(event)
    started = pending_restore.get(key, 0)
    if not started or time.time() - started > RESTORE_TTL:
        pending_restore.pop(key, None)
        return
    if not _is_admin(event):
        return
    if Path(event.file.name).suffix.lower() != ".zip":
        await bot.send(event, "恢复文件必须是 zip 格式")
        return
    if event.file.size > MAX_BACKUP_BYTES:
        await bot.send(event, "备份文件不能超过 512 MB")
        return
    try:
        result = await bot.call_api(
            "get_group_file_url",
            group_id=event.group_id,
            file_id=event.file.id,
            busid=event.file.busid,
        )
        source = str(result.get("url") or "") if isinstance(result, dict) else ""
        if not source:
            raise ValueError("OneBot 未返回群文件下载地址")
        message = await _restore_source(source, event.file.name)
    except Exception as error:
        logger.exception("从群文件恢复表情包失败")
        await bot.send(event, f"恢复失败：{error}")
        return
    pending_restore.pop(key, None)
    await bot.send(event, message)
