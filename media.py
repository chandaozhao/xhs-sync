"""M4 媒体下载：把笔记的图片、视频下载到 <Vault>/RedNote/Media/<笔记id>/。

下载地址来自详情页 __INITIAL_STATE__ 里的媒体地址（小红书 CDN：xhscdn.com），
相当于浏览器显示图片/播放视频时会请求的文件。地址带有效期，所以打开详情页后立刻下载。
"""
from __future__ import annotations

import random
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PlaywrightError

from xhs_common import StopSync

REFERER = "https://www.xiaohongshu.com/"
CONTENT_TYPE_EXT = {
    "image/webp": "webp", "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/heic": "heic", "image/avif": "avif", "video/mp4": "mp4",
}


class MediaError(Exception):
    pass


def _fetch(ctx: BrowserContext, urls: list[str]) -> tuple[bytes, str | None]:
    """依次尝试主地址和备用地址，返回 (文件内容, 按 Content-Type 得到的扩展名)。"""
    last = "没有可用地址"
    for url in urls:
        try:
            resp = ctx.request.get(url, headers={"Referer": REFERER}, timeout=180_000)
        except PlaywrightError as e:
            last = str(e).splitlines()[0]
            continue
        if resp.status == 429:
            raise StopSync("下载媒体时被限流（HTTP 429 请求太频繁），先停下，过段时间再试。")
        if resp.status != 200:
            last = f"HTTP {resp.status}"
            continue
        body = resp.body()
        if not body:
            last = "下载到的是空文件"
            continue
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        return body, CONTENT_TYPE_EXT.get(ctype)
    raise MediaError(last)


def download_media(ctx: BrowserContext, plan: list[dict], folder: Path) -> tuple[list[dict], list[str]]:
    """按 plan 下载媒体。返回 (已保存的媒体清单, 错误信息列表)。

    已经下载过的文件（同名、非空）直接复用，失败重试时不会重复下载。
    """
    folder.mkdir(parents=True, exist_ok=True)
    saved, errors = [], []
    for item in plan:
        stem, guessed_ext = item["filename"].rsplit(".", 1)
        existing = [p for p in folder.glob(f"{stem}.*")
                    if not p.name.endswith(".part") and p.stat().st_size > 0]
        if existing:
            saved.append({**item, "filename": existing[0].name})
            continue
        try:
            body, ext = _fetch(ctx, [item["url"], *item["backup_urls"]])
        except MediaError as e:
            errors.append(f"{item['filename']}（{e}）")
            continue
        name = f"{stem}.{ext or guessed_ext}"
        part = folder / f"{name}.part"
        part.write_bytes(body)
        part.replace(folder / name)  # 下载完整后才改成正式文件名
        saved.append({**item, "filename": name})
        time.sleep(random.uniform(0.3, 1.0))  # 文件之间稍微停一下
    return saved, errors
