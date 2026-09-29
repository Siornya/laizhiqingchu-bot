from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from nonebot import logger

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
RESERVED_KEYWORDS = {
    "添加",
    "批量添加合并转发",
    "来只",
    "列表",
    "删图",
    "删除",
    "统计",
    "创建备份",
    "恢复备份",
    "清空图库",
    "表情包管理菜单",
    "屏蔽",
    "屏蔽列表",
    "解除屏蔽",
}
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
MAX_RESTORE_UNPACKED_BYTES = 1024 * 1024 * 1024


def valid_keyword(keyword: str) -> bool:
    if not keyword or keyword in RESERVED_KEYWORDS:
        return False
    if any(char.isspace() for char in keyword):
        return False
    if keyword in {".", ".."} or "/" in keyword or "\\" in keyword:
        return False
    if any(char in keyword for char in '<>:"|?*'):
        return False
    if keyword.split(".")[0].upper() in WINDOWS_RESERVED:
        return False
    return keyword == keyword.rstrip(" .")


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - used for duplicate detection only
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 - duplicate detection only


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default
    return value


class StickerStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        default_limit_mb: int = 200,
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.index_path = self.data_dir / "index.json"
        self.blocked_path = self.data_dir / "blocked_keywords.json"
        self.backup_dir = self.data_dir.parent / "sticker_backups"
        self.index: dict[str, list[str]] = {}
        self.blocked_keywords: set[str] = set()
        self.max_storage_mb = max(1, int(default_limit_mb))
        self.load()

    def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw_index = _read_json(self.index_path, {})
        if isinstance(raw_index, dict):
            self.index = {
                str(keyword): [str(name) for name in names if isinstance(name, str)]
                for keyword, names in raw_index.items()
                if valid_keyword(str(keyword)) and isinstance(names, list)
            }
        raw_blocked = _read_json(self.blocked_path, [])
        if isinstance(raw_blocked, list):
            self.blocked_keywords = {
                str(keyword) for keyword in raw_blocked if valid_keyword(str(keyword))
            }
        self.prune_all()

    def save_index(self) -> None:
        _atomic_json(self.index_path, self.index)

    def save_blocked(self) -> None:
        _atomic_json(self.blocked_path, sorted(self.blocked_keywords))

    def prune(self, keyword: str) -> list[str]:
        files = self.index.get(keyword, [])
        existing = [name for name in files if (self.data_dir / keyword / name).is_file()]
        if existing != files:
            if existing:
                self.index[keyword] = existing
            else:
                self.index.pop(keyword, None)
            self.save_index()
        return existing

    def prune_all(self) -> None:
        changed = False
        for keyword, files in list(self.index.items()):
            existing = [
                name for name in files if (self.data_dir / keyword / name).is_file()
            ]
            if existing != files:
                changed = True
                if existing:
                    self.index[keyword] = existing
                else:
                    self.index.pop(keyword, None)
        if changed:
            self.save_index()

    def add_images(self, keyword: str, images: list[tuple[bytes, str]]) -> tuple[int, int]:
        folder = self.data_dir / keyword
        folder.mkdir(parents=True, exist_ok=True)
        existing_hashes = {file_md5(path) for path in folder.iterdir() if path.is_file()}
        added = skipped = 0
        for content, suffix in images:
            digest = bytes_md5(content)
            if digest in existing_hashes:
                skipped += 1
                continue
            suffix = suffix.lower()
            if suffix not in ALLOWED_EXTS:
                suffix = ".jpg"
            filename = f"{time.time_ns()}_{secrets.randbelow(9000) + 1000}{suffix}"
            target = folder / filename
            target.write_bytes(content)
            self.index.setdefault(keyword, []).append(filename)
            existing_hashes.add(digest)
            added += 1
        self.save_index()
        self.warn_if_over_limit()
        return added, skipped

    def delete_by_hashes(
        self, hashes: set[str], keyword: str | None = None
    ) -> dict[str, int]:
        targets = [keyword] if keyword else list(self.index)
        result: dict[str, int] = {}
        for current in targets:
            folder = self.data_dir / current
            if not folder.is_dir():
                continue
            deleted = 0
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                try:
                    matches = file_md5(path) in hashes
                except OSError:
                    continue
                if matches:
                    path.unlink(missing_ok=True)
                    deleted += 1
            if deleted:
                result[current] = deleted
                self.prune(current)
        return result

    def delete_index(self, keyword: str, index: int) -> str:
        files = self.prune(keyword)
        if index < 1 or index > len(files):
            raise IndexError(index)
        filename = files.pop(index - 1)
        (self.data_dir / keyword / filename).unlink(missing_ok=True)
        if files:
            self.index[keyword] = files
        else:
            self.index.pop(keyword, None)
        self.save_index()
        return filename

    def delete_keyword(self, keyword: str) -> int:
        count = len(self.prune(keyword))
        shutil.rmtree(self.data_dir / keyword, ignore_errors=True)
        self.index.pop(keyword, None)
        self.save_index()
        return count

    def clear(self) -> tuple[int, int]:
        keywords = len(self.index)
        images = sum(len(files) for files in self.index.values())
        for child in self.data_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
        self.index.clear()
        self.save_index()
        return keywords, images

    def stats(self) -> tuple[int, int, list[tuple[str, int]]]:
        self.prune_all()
        total = sum(len(files) for files in self.index.values())
        top = sorted(
            ((keyword, len(files)) for keyword, files in self.index.items()),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        return len(self.index), total, top

    def storage_bytes(self) -> int:
        total = 0
        for path in self.data_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def warn_if_over_limit(self) -> None:
        total = self.storage_bytes()
        if total > self.max_storage_mb * 1024 * 1024:
            logger.warning(
                "表情包存储已超过 {} MB（当前 {:.1f} MB）",
                self.max_storage_mb,
                total / 1024 / 1024,
            )

    def create_backup(self) -> dict[str, Any]:
        self.prune_all()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"sticker_backup_{time.strftime('%Y%m%d_%H%M%S')}_"
            f"{secrets.randbelow(9000) + 1000}.zip"
        )
        target = self.backup_dir / filename
        files_meta: list[dict[str, str]] = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "index.json", json.dumps(self.index, ensure_ascii=False, indent=2)
            )
            archive.writestr(
                "blocked_keywords.json",
                json.dumps(sorted(self.blocked_keywords), ensure_ascii=False, indent=2),
            )
            for keyword, filenames in self.index.items():
                for filename_in_pack in filenames:
                    source = self.data_dir / keyword / filename_in_pack
                    if source.is_file():
                        archive_name = f"{keyword}/{filename_in_pack}"
                        archive.write(source, archive_name)
                        files_meta.append({"path": archive_name, "md5": file_md5(source)})
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "keywords": len(self.index),
                        "images": len(files_meta),
                        "files": files_meta,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return {
            "filename": filename,
            "size": target.stat().st_size,
            "keywords": len(self.index),
            "images": len(files_meta),
        }

    def list_backups(self) -> list[dict[str, Any]]:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        return [
            {
                "filename": path.name,
                "size": path.stat().st_size,
                "created": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)
                ),
            }
            for path in sorted(self.backup_dir.glob("sticker_backup_*.zip"), reverse=True)
        ]

    def backup_path(self, name: str) -> Path | None:
        target = (self.backup_dir / Path(name).name).resolve()
        if (
            target.parent != self.backup_dir.resolve()
            or target.suffix.lower() != ".zip"
            or not target.is_file()
        ):
            return None
        return target

    def restore(self, zip_path: Path) -> dict[str, Any]:
        try:
            archive = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile as error:
            raise ValueError("压缩包已损坏或不是有效的 zip 文件") from error
        with archive:
            broken = archive.testzip()
            if broken:
                raise ValueError(f"压缩包已损坏：{broken}")
            unpacked_size = sum(item.file_size for item in archive.infolist())
            if unpacked_size > MAX_RESTORE_UNPACKED_BYTES:
                raise ValueError("备份解压后不能超过 1 GB")
            members = set(archive.namelist())
            if not {"index.json", "manifest.json"}.issubset(members):
                raise ValueError("压缩包缺少 index.json 或 manifest.json")
            manifest = json.loads(archive.read("manifest.json"))
            archive_index = json.loads(archive.read("index.json"))
            if manifest.get("version") != 1 or not isinstance(archive_index, dict):
                raise ValueError("不支持的备份格式")

            manifest_files = manifest.get("files")
            if not isinstance(manifest_files, list):
                raise ValueError("manifest 缺少文件校验清单")
            expected_hashes = {
                str(item.get("path")): str(item.get("md5"))
                for item in manifest_files
                if isinstance(item, dict) and item.get("path") and item.get("md5")
            }

            expected: set[str] = set()
            for keyword, filenames in archive_index.items():
                if not valid_keyword(str(keyword)) or not isinstance(filenames, list):
                    raise ValueError(f"索引包含非法关键词：{keyword}")
                for filename in filenames:
                    path = PurePosixPath(str(filename))
                    if len(path.parts) != 1 or path.name != str(filename):
                        raise ValueError(f"索引包含非法文件名：{filename}")
                    expected.add(f"{keyword}/{filename}")
            if len(expected) != int(manifest.get("images", -1)):
                raise ValueError("manifest 图片数量与索引不一致")
            if set(expected_hashes) != expected:
                raise ValueError("manifest 文件清单与索引不一致")
            if len(archive_index) != int(manifest.get("keywords", -1)):
                raise ValueError("manifest 关键词数量与索引不一致")
            fixed = {"index.json", "manifest.json", "blocked_keywords.json"}
            for member in members - fixed:
                path = PurePosixPath(member)
                if member not in expected or ".." in path.parts or len(path.parts) != 2:
                    raise ValueError(f"压缩包包含非法或未索引路径：{member}")
            if not expected.issubset(members):
                raise ValueError("压缩包缺少索引引用的图片")

            for archive_name, expected_hash in expected_hashes.items():
                if bytes_md5(archive.read(archive_name)) != expected_hash:
                    raise ValueError(f"文件校验失败：{archive_name}")

            added = skipped = overwritten = failed = 0
            for keyword, filenames in archive_index.items():
                folder = self.data_dir / keyword
                folder.mkdir(parents=True, exist_ok=True)
                for filename in filenames:
                    try:
                        content = archive.read(f"{keyword}/{filename}")
                        target = folder / filename
                        if target.is_file():
                            if bytes_md5(content) == file_md5(target):
                                skipped += 1
                                if filename not in self.index.setdefault(keyword, []):
                                    self.index[keyword].append(filename)
                                continue
                            overwritten += 1
                        else:
                            added += 1
                        target.write_bytes(content)
                        if filename not in self.index.setdefault(keyword, []):
                            self.index[keyword].append(filename)
                    except (OSError, KeyError) as error:
                        failed += 1
                        logger.error("恢复图片 {}/{} 失败：{}", keyword, filename, error)
            if "blocked_keywords.json" in members:
                blocked = json.loads(archive.read("blocked_keywords.json"))
                if isinstance(blocked, list):
                    self.blocked_keywords.update(
                        str(item) for item in blocked if valid_keyword(str(item))
                    )
                    self.save_blocked()
            self.save_index()
        return {
            "archive_keywords": len(archive_index),
            "archive_images": len(expected),
            "added": added,
            "skipped": skipped,
            "overwritten": overwritten,
            "failed": failed,
            "message": (
                f"恢复完成：新增 {added} 张，覆盖 {overwritten} 张，"
                f"跳过重复 {skipped} 张，失败 {failed} 张"
            ),
        }
