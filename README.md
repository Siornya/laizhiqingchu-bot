# NoneBot 表情包管理

这是从 AstrBot `sticker_plugin` 移植而来的 **NoneBot2 + OneBot V11** 插件。它以关键词组织本地表情包，支持回复收图、MD5 去重、随机发送、精准删除、合并转发批量导入和消息备份恢复。

原项目作者为 `td1336065617`；本仓库保留 MIT 许可证，并在其设计基础上完成 NoneBot 适配。

## 保留的功能

- 回复图片或在命令中附图：`添加{关键词}`
- 90 秒内发送合并转发批量入库：`批量添加合并转发{关键词}`
- 随机发送：`来只{关键词}`
- 同关键词内按图片内容 MD5 去重
- 管理员分页列表、按回复图片精准删除、按序号删除
- 删除整个关键词时 60 秒二次确认
- 关键词屏蔽、统计、存储容量告警
- 通过聊天文件创建备份、校验并合并恢复
- 通过管理员消息二次确认清空图库
- 原 AstrBot `stickers/` 数据目录和备份 zip 格式可直接迁移

没有保留 QQ 官方机器人适配和 WebUI；本移植版专注 OneBot V11，所有管理操作均通过消息完成。

## 安装

```bash
pip install git+https://github.com/Siornya/laizhiqingchu-bot.git
```

在 NoneBot 配置中加载插件：

```toml
plugins = ["nonebot_plugin_meme_manager"]
```

需要使用 `nonebot-adapter-onebot`，并让协议端支持 OneBot V11 的图片消息；合并转发导入还要求实现 `get_forward_msg` API。

## 指令

指令可带 `/`，也可直接发送中文前缀。关键词不能包含空格或路径字符。

| 权限 | 指令 | 说明 |
| --- | --- | --- |
| 所有人 | `添加{关键词}` | 回复图片或随消息附图，批量入库 |
| 所有人 | `批量添加合并转发{关键词}` | 进入 90 秒等待状态，下一条合并转发批量入库 |
| 所有人 | `来只{关键词}` | 随机发送一张图片 |
| 所有人 | `表情包管理菜单` | 显示菜单；有中文字体和 Pillow 时优先发图片 |
| 管理员 | `列表{关键词}{页码}` | 每页列出 10 张图片 |
| 管理员 | `删图` | 回复图片，按 MD5 在全库精准删除 |
| 管理员 | `删图{关键词}` | 回复图片，只在指定关键词中删除 |
| 管理员 | `删图{关键词}{序号}` | 按列表序号删除 |
| 管理员 | `删除{关键词}` | 60 秒内再次发送，删除整个关键词 |
| 管理员 | `统计` | 显示关键词数、图片总数和 Top 3 |
| 管理员 | `屏蔽{关键词}` | 禁止添加、发送和查看此关键词 |
| 管理员 | `解除屏蔽{关键词}` | 解除屏蔽 |
| 管理员 | `屏蔽列表` | 查看屏蔽关键词 |
| 管理员 | `创建备份` | 生成 zip，并通过群文件或私聊文件发送 |
| 管理员 | `恢复备份` | 进入 120 秒等待状态，随后上传备份 zip |
| 管理员 | `清空图库` | 60 秒内再次发送相同指令确认清空 |

管理员仅使用 NoneBot `.env` 中的 `SUPERUSERS`。

## 配置

在 `.env` 或对应环境配置文件中设置：

```dotenv
STICKER_MAX_STORAGE_MB=200
STICKER_HTTP_TIMEOUT=20
STICKER_MAX_IMAGE_MB=20

# 留空时使用 nonebot-plugin-localstore 的插件数据目录
STICKER_DATA_DIR=
```

`STICKER_MAX_STORAGE_MB` 只从 NoneBot 的 `.env` 或当前环境配置文件读取，修改后需要重启机器人。插件不会再用数据目录中的设置文件覆盖它。达到上限时会写入警告日志，管理员也可用 `统计` 查看当前占用。

### 消息备份

发送 `创建备份` 后，机器人会生成兼容 zip 并作为群文件或私聊文件发送。发送 `恢复备份` 后，在 120 秒内上传该 zip；恢复采用合并模式，同名且内容相同的图片会跳过。群文件功能要求 OneBot 实现支持 `upload_group_file` 和 `get_group_file_url`，私聊文件要求支持 `upload_private_file` 及文件下载地址。

## 从 AstrBot 迁移

两种方式任选其一：

1. 将 AstrBot 的 `data/stickers/` 整个目录复制到本插件数据位置，再启动 NoneBot。
2. 在旧插件 WebUI 生成 zip 备份，向机器人发送 `恢复备份`，随后在聊天中上传该文件。

默认数据位置由 `nonebot-plugin-localstore` 决定。可先启动一次，从实际生成目录确认位置；也可用 `STICKER_DATA_DIR` 显式指定到旧 `stickers/` 目录。

```text
stickers/
├── index.json
├── blocked_keywords.json
├── 猫猫/
│   └── 1710000000000_1234.png
└── ...
```

## 开发

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

## 许可

MIT，详见 `LICENSE`。
