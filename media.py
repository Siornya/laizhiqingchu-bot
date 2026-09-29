from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from .storage import ALLOWED_EXTS


def image_segments(event: MessageEvent) -> list[MessageSegment]:
    reply = getattr(event, "reply", None)
    if reply is not None:
        images = [segment for segment in reply.message if segment.type == "image"]
        if images:
            return images
    return [segment for segment in event.message if segment.type == "image"]


def file_segments(event: MessageEvent) -> list[MessageSegment]:
    reply = getattr(event, "reply", None)
    if reply is not None:
        files = [segment for segment in reply.message if segment.type == "file"]
        if files:
            return files
    return [segment for segment in event.message if segment.type == "file"]


def is_forward_message(message: Message) -> bool:
    return any(segment.type in {"forward", "node"} for segment in message)


def _suffix_from(content_type: str, source: str) -> str:
    suffix = Path(urlparse(source).path).suffix.lower()
    if suffix in ALLOWED_EXTS:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    return guessed if guessed in ALLOWED_EXTS else ".jpg"


async def image_content(
    bot: Bot,
    segment: MessageSegment,
    *,
    timeout: float,
    max_bytes: int,
) -> tuple[bytes, str]:
    data = segment.data
    url = str(data.get("url") or "").strip()
    file_value = str(data.get("file") or "").strip()
    source = url or file_value
    if file_value.startswith("base64://"):
        try:
            return base64.b64decode(file_value[9:], validate=True), ".jpg"
        except (ValueError, binascii.Error) as error:
            raise ValueError("图片 base64 数据无效") from error
    if source.startswith("file://"):
        local = Path(unquote(urlparse(source).path))
        content = await asyncio.to_thread(local.read_bytes)
        if len(content) > max_bytes:
            raise ValueError("图片超过大小限制")
        return content, local.suffix.lower() or ".jpg"
    if source.startswith(("http://", "https://")):
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            response = await client.get(source)
            response.raise_for_status()
        if len(response.content) > max_bytes:
            raise ValueError("图片超过大小限制")
        return response.content, _suffix_from(
            response.headers.get("content-type", ""), source
        )
    local = Path(source)
    if source and await asyncio.to_thread(local.is_file):
        content = await asyncio.to_thread(local.read_bytes)
        if len(content) > max_bytes:
            raise ValueError("图片超过大小限制")
        return content, local.suffix.lower() or ".jpg"
    if file_value:
        try:
            info = await bot.get_image(file=file_value)
            resolved = str(info.get("file") or info.get("url") or "")
        except Exception as error:
            raise ValueError(f"OneBot 无法取得图片：{error}") from error
        if resolved and resolved != source:
            return await image_content(
                bot,
                MessageSegment.image(resolved),
                timeout=timeout,
                max_bytes=max_bytes,
            )
    raise ValueError("消息段不含可读取的图片地址")


def _file_source(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("url", "path", "file"):
        source = str(value.get(key) or "").strip()
        if source.startswith(("http://", "https://", "file://")):
            return source
        if source and Path(source).is_file():
            return source
    nested = value.get("data")
    return _file_source(nested) if isinstance(nested, dict) else ""


async def message_file_source(bot: Bot, segment: MessageSegment) -> tuple[str, str]:
    name = str(segment.data.get("name") or segment.data.get("filename") or "backup.zip")
    if source := _file_source(segment.data):
        return source, name
    file_id = str(
        segment.data.get("file_id")
        or segment.data.get("id")
        or segment.data.get("file")
        or ""
    )
    if not file_id:
        raise ValueError("文件消息中没有可用的文件标识")
    attempts = (
        ("get_file", {"file_id": file_id}),
        ("get_private_file_url", {"file_id": file_id}),
    )
    for api, params in attempts:
        try:
            result = await bot.call_api(api, **params)
        except Exception as error:
            logger.debug("OneBot 接口 {} 未返回文件地址：{}", api, error)
            continue
        if source := _file_source(result):
            return source, name
    raise ValueError("OneBot 实现未提供私聊文件下载地址")


async def download_file(
    source: str,
    target: Path,
    *,
    timeout: float,
    max_bytes: int,
) -> None:
    if source.startswith("file://"):
        local = Path(unquote(urlparse(source).path))
    elif source.startswith(("http://", "https://")):
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", source) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise ValueError("备份文件超过大小限制")
                written = 0
                with target.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError("备份文件超过大小限制")
                        file.write(chunk)
        return
    else:
        local = Path(source)
    if not await asyncio.to_thread(local.is_file):
        raise ValueError("备份文件不存在")
    if (await asyncio.to_thread(local.stat)).st_size > max_bytes:
        raise ValueError("备份文件超过大小限制")
    await asyncio.to_thread(shutil.copyfile, local, target)


def _walk_forward(value: Any, images: list[MessageSegment]) -> None:
    if isinstance(value, MessageSegment):
        if value.type == "image":
            images.append(value)
        else:
            _walk_forward(value.data, images)
        return
    if isinstance(value, Message):
        for segment in value:
            _walk_forward(segment, images)
        return
    if isinstance(value, dict):
        segment_type = str(value.get("type") or "").lower()
        if segment_type == "image":
            images.append(MessageSegment("image", dict(value.get("data") or {})))
            return
        for key in ("messages", "message", "content", "nodes", "data"):
            if key in value:
                _walk_forward(value[key], images)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_forward(item, images)


async def forward_images(bot: Bot, message: Message) -> list[MessageSegment]:
    images: list[MessageSegment] = []
    for segment in message:
        if segment.type == "image":
            images.append(segment)
        elif segment.type == "node":
            _walk_forward(segment.data, images)
        elif segment.type == "forward":
            forward_id = str(segment.data.get("id") or "")
            if not forward_id:
                continue
            try:
                response = await bot.call_api("get_forward_msg", message_id=forward_id)
            except Exception:
                try:
                    response = await bot.call_api("get_forward_msg", id=forward_id)
                except Exception as error:
                    logger.warning("读取合并转发 {} 失败：{}", forward_id, error)
                    continue
            _walk_forward(response, images)
    return images
