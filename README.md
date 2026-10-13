# xhs-sync

把**你自己**小红书账号的「收藏」定期同步到 Obsidian，并把收藏里的视频在本机转成文字稿。

收藏夹里的笔记会被删、会找不到，视频也只能一个个点开看。xhs-sync 把它们变成你自己硬盘上的 Markdown 笔记：能搜索、能打标签、原笔记删了也还在；视频的文字稿就放在笔记里，不开声音也能「看」。

> 💡 思路来自小红书博主 **unintended.ai**（小红书号：1511342664）开源的 Obsidian 插件 [xhs2obsidian](https://github.com/unintended0x/xhs2obsidian)。本项目是照着这个思路，用 Claude Code vibe coding 出来的独立实现（Python 脚本），另外加上了视频本地转文字稿和定时防风控。感谢原作者的开源分享！

> ⚠️ 这是个人备份工具：只保存你自己账号的收藏，内容版权归原作者，请勿二次传播或商用。自动化访问可能不符合平台规则，请自行评估风险。

## 功能

- **一篇收藏 = 一篇 Markdown 笔记**：标题、正文、作者、话题标签、发布时间、点赞 / 收藏 / 评论数、原文链接写进 frontmatter（Obsidian 的「属性」）。
- **图片和视频下载到本地**：存在 `<Vault>/RedNote/Media/<笔记id>/`，笔记里用相对路径嵌入。
- **视频转文字稿**：whisper.cpp + large-v3-turbo 模型，在 Mac 上本地转录（GPU 加速），不上传任何数据。输出简体中文、带标点、自动分段；纯音乐视频里 AI「脑补」出的假字幕会被过滤。文字稿放在视频下方默认折叠的区块里。
- **增量同步**：第一次从新到旧慢慢补齐旧收藏（每次 20 篇），补齐后每次只同步新收藏；失败自动重试，3 次后放弃。
- **读完就删**：在 Obsidian 里删掉的笔记不会被重新下载，它的图片和视频会自动移到废纸篓（可恢复）。
- **定时自动运行**：可以每天定时同步；遇到验证码等情况会自动暂停并弹通知。

## 怎么做到的（以及为什么尽量不给账号添麻烦）

- 用 Playwright 打开一个真实的浏览器，登录状态保存在你本机（`~/.xhs-sync/profile`）。
- **只读取浏览器正常加载页面得到的数据**（页面里的 `__INITIAL_STATE__`），相当于你自己在电脑上翻收藏夹。**不逆向接口、不计算签名、不伪造请求**，打开笔记只用收藏卡片上的链接。
- **慢**：每篇笔记之间随机等 20–40 秒，每次最多 20 篇。
- **遇到问题立刻停**：验证码 / 滑块、登录页、「访问频繁」、HTTP 461 / 471 / 429，一律立刻停止并提示你处理，不尝试绕过。
- 不使用任何反检测 / 伪装插件。
- 媒体文件从小红书的 CDN（xhscdn.com）下载，打开详情页后立刻下载（地址有有效期）。

## 需要什么

- 一台 Mac（转录推荐 Apple 芯片；macOS 14 或更新，自动清理要用系统自带的 `trash` 命令）
- [Obsidian](https://obsidian.md)，和一个 Vault（仓库）
- [Homebrew](https://brew.sh)（用来安装 ffmpeg 和 whisper.cpp；不装也能同步，只是不能转文字）
- 大约 2 GB 空间（浏览器约 150 MB，语音模型约 1.6 GB）

## 安装

1. 下载本项目（Release 页面的 Source code，或 `git clone`），解压后在「终端」里进入项目文件夹。
2. 运行安装脚本（创建 Python 环境、下载浏览器、安装 ffmpeg / whisper.cpp、下载语音模型，需要一些时间）：

   ```bash
   bash setup.sh
   ```

3. 用文本编辑打开项目里的 `config.toml`，填写 `vault_path`（你的 Obsidian Vault 路径）。不知道路径的话，把 Vault 文件夹从 Finder 拖进「终端」窗口就能看到。
4. 第一次运行，在弹出的浏览器里用小红书 App 扫码登录：

   ```bash
   .venv/bin/python sync.py --show --limit 2
   ```

   成功后去 Obsidian 里看看 `RedNote/Bookmarks/`。

## 日常使用

```bash
.venv/bin/python sync.py            # 同步（默认不弹出浏览器窗口）
.venv/bin/python sync.py --show     # 显示浏览器窗口；登录过期需要重新扫码时用这个
.venv/bin/python sync.py --limit 5  # 这次只同步 5 篇
```

- 随时可以按 `Ctrl + C` 中断，已完成的进度都已保存。
- 同步结束后会自动转录新视频（`config.toml` 的 `auto_transcribe`）。

### 视频转文字

```bash
.venv/bin/python transcribe.py                  # 转录所有还没转录的视频
.venv/bin/python transcribe.py --limit 3        # 这次只转 3 个
.venv/bin/python transcribe.py --redo <笔记id>   # 重新转录某几篇（id 在笔记顶部，多个用空格隔开）
```

- 转好的笔记顶部会多一行 `transcribed: true`，不会重复转录。文字稿区块可以随便改，删掉也不会被加回来。
- 速度参考（M4）：3 分钟的视频约 15 秒，30 分钟的约 3 分钟。
- 视频是英文的：把 `transcribe_language` 改成 `"en"` 或 `"auto"`。

### 定时自动同步

用 `sync.py --scheduled` 运行时会多几条保护规则（见 `autorun.py`）：

- 每次开始前随机推迟 0–50 分钟；旧收藏补齐后每天只同步 1 次。
- 遇到验证码、访问频繁、登录失效：写入 `paused.txt`，暂停之后所有定时同步，并弹出 Mac 通知。手动运行一次 `sync.py --show` 成功后自动恢复。
- 网络不通只跳过这一次；正在手动同步时自动跳过。

可以用任意定时工具每天跑几次 `.venv/bin/python -u sync.py --scheduled`，比如 Claude 桌面 App 的定时任务。

> 注意：如果 Vault 在 iCloud 里，macOS 默认不允许 launchd / cron 这类后台程序读取 iCloud 云盘（会读到「空文件夹」）。程序开头会检查，读不了就停下，不会写出重复笔记。用 launchd / cron 的话需要给 Python 开「完全磁盘访问权限」。

### 在 Obsidian 里删掉读完的笔记

- 直接删就行，不会被重新下载。笔记挪到 Vault 里别的文件夹也没关系，程序按笔记里的 `id` 认。
- 下次同步结束时，被删笔记的图片和视频会自动移到废纸篓。为了安全：iCloud 里还有笔记没下载到本机、或一下子「不见」了一半以上的笔记时，这次不会自动删。
- 不想自动删：把 `auto_clean_media` 改成 `false`，改用 `.venv/bin/python cleanup.py` 手动确认后清理。

## 文件说明

| 文件 | 作用 |
|---|---|
| `sync.py` | 主程序：收藏列表 → 逐篇详情 → 下载媒体 → 写 Markdown → 记录进度 → 自动清理 → 转录 |
| `favorites.py` | 打开「我 → 收藏」，边滚动边收集笔记卡片 |
| `note_detail.py` | 读取笔记详情，生成 Markdown |
| `media.py` | 下载图片 / 视频 |
| `transcribe.py` | 视频转文字（ffmpeg + whisper.cpp） |
| `state.py` | 同步记录 `state.json`（自动在 Vault 里备份一份） |
| `cleanup.py` | 清理已删除笔记的媒体 |
| `autorun.py` | 定时同步的规则；直接运行查看状态 |
| `xhs_common.py` | 配置、浏览器、登录检测、拦截检测 |
| `explore.py` / `explore_detail.py` | 开发用的探索脚本，页面数据存到 `debug/` |
| `config.example.toml` | 配置模板 |

`state.json`、`config.toml`、`debug/` 里是你自己的数据，已在 `.gitignore` 里排除，不要分享给别人。

## 局限

- 只支持 macOS。
- 语音识别会有错别字；背景音乐里的歌词也会被转出来。
- 小红书页面改版可能导致失效，需要根据新的页面结构调整（可以先用 `explore.py` 看真实数据）。

## 致谢

- [xhs2obsidian](https://github.com/unintended0x/xhs2obsidian)（小红书博主 unintended.ai）：本项目的思路来源，`RedNote/Bookmarks` 目录结构和笔记属性也沿用了它的约定。
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp)、[Playwright](https://playwright.dev)、[ffmpeg](https://ffmpeg.org)、[Obsidian](https://obsidian.md)。

## License

[MIT](LICENSE)
