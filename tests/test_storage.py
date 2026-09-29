from __future__ import annotations

import json
import zipfile

import pytest
from nonebot_plugin_meme_manager.storage import StickerStore, valid_keyword


def test_storage_limit_is_not_overridden_by_legacy_settings(tmp_path) -> None:
    (tmp_path / "sticker_settings.json").write_text(
        '{"max_storage_mb": 999}', encoding="utf-8"
    )
    store = StickerStore(tmp_path / "stickers", default_limit_mb=123)
    assert store.max_storage_mb == 123


@pytest.mark.parametrize("keyword", ["猫猫", "dog", "com10", "a-b_c", "开心.png"])
def test_valid_keyword_accepts_safe_names(keyword: str) -> None:
    assert valid_keyword(keyword)


@pytest.mark.parametrize(
    "keyword", ["", "猫 猫", "../猫", "CON", "con.txt", "LPT9", "猫猫.", "添加"]
)
def test_valid_keyword_rejects_unsafe_names(keyword: str) -> None:
    assert not valid_keyword(keyword)


def test_add_deduplicate_and_delete(tmp_path) -> None:
    store = StickerStore(tmp_path / "stickers")
    first = b"first image"
    second = b"second image"

    assert store.add_images("猫猫", [(first, ".png"), (first, ".jpg")]) == (1, 1)
    assert store.add_images("猫猫", [(second, ".gif")]) == (1, 0)
    assert len(store.prune("猫猫")) == 2

    filename = store.delete_index("猫猫", 1)
    assert filename.endswith(".png")
    assert len(store.prune("猫猫")) == 1


def test_backup_restore_round_trip(tmp_path) -> None:
    source = StickerStore(tmp_path / "source" / "stickers")
    source.add_images("猫猫", [(b"cat", ".png"), (b"cat2", ".gif")])
    source.blocked_keywords.add("冻结")
    source.save_blocked()

    info = source.create_backup()
    backup = source.backup_path(info["filename"])
    assert backup is not None

    target = StickerStore(tmp_path / "target" / "stickers")
    result = target.restore(backup)
    assert result["added"] == 2
    assert len(target.prune("猫猫")) == 2
    assert "冻结" in target.blocked_keywords

    repeated = target.restore(backup)
    assert repeated["skipped"] == 2


def test_restore_rejects_unindexed_path(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("index.json", json.dumps({"猫猫": ["a.png"]}))
        output.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "keywords": 1,
                    "images": 1,
                    "files": [
                        {"path": "猫猫/a.png", "md5": "d077f244def8a70e5ea758bd8352fcd8"}
                    ],
                }
            ),
        )
        output.writestr("猫猫/a.png", b"cat")
        output.writestr("../escape.txt", b"bad")

    store = StickerStore(tmp_path / "stickers")
    with pytest.raises(ValueError, match="非法"):
        store.restore(archive)


def test_restore_rejects_hash_mismatch_before_writing(tmp_path) -> None:
    archive = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("index.json", json.dumps({"猫猫": ["a.png"]}))
        output.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "keywords": 1,
                    "images": 1,
                    "files": [{"path": "猫猫/a.png", "md5": "not-the-real-hash"}],
                }
            ),
        )
        output.writestr("猫猫/a.png", b"cat")

    store = StickerStore(tmp_path / "stickers")
    with pytest.raises(ValueError, match="校验失败"):
        store.restore(archive)
    assert store.index == {}
