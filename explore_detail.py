"""M3 第一步：探索笔记详情页。

从收藏列表里挑 1 篇视频笔记和 1 篇图文笔记，用卡片上的 href 打开详情页，
把 __INITIAL_STATE__、接口响应、页面 HTML、截图、加载的图片/视频地址存到 debug/。
两篇之间按 config.toml 的间隔随机等待（默认 20–40 秒）。

运行：.venv/bin/python explore_detail.py
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, Response, sync_playwright

from explore import Explorer
from favorites import collect_favorites, open_favorites
from xhs_common import (
    DEBUG_DIR, ResponseRecorder, StopSync, first_page, goto, human_pause, load_config,
    open_browser, stop_if_blocked,
)


class MediaRecorder:
    """记录页面加载的图片/视频地址（只记地址，不下载）。"""

    def __init__(self, page):
        self.phase = "start"
        self.items: list[dict] = []
        page.on("response", self._on_response)

    def _on_response(self, resp: Response) -> None:
        try:
            if resp.request.resource_type not in ("image", "media"):
                return
            host = urlparse(resp.url).hostname or ""
            self.items.append({
                "phase": self.phase, "type": resp.request.resource_type, "host": host,
                "status": resp.status, "content_type": resp.headers.get("content-type", ""),
                "url": resp.url,
            })
        except Exception:
            pass


def pick_targets(items: list[dict]) -> list[dict]:
    """挑第一篇视频笔记和第一篇非视频笔记。"""
    video = next((i for i in items if i["type"] == "video" and i["href"]), None)
    other = next((i for i in items if i["type"] != "video" and i["href"]), None)
    return [t for t in (video, other) if t]


def main() -> int:
    cfg = load_config()
    out = DEBUG_DIR / datetime.now().strftime("detail-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"探索结果会保存到：{out}")

    with sync_playwright() as pw:
        ctx = open_browser(pw, cfg, headless=False)
        page = first_page(ctx)
        rec = ResponseRecorder(page)
        media = MediaRecorder(page)
        ex = Explorer(page, rec, out)
        try:
            open_favorites(page, rec, interactive=True)
            items, _ = collect_favorites(page, rec, limit=20)
            targets = pick_targets(items)
            ex.note(f"挑选的笔记：{[(t['id'], t['type'], t['title']) for t in targets]}")
            (out / "targets.json").write_text(json.dumps(targets, ensure_ascii=False, indent=1), "utf-8")

            for n, t in enumerate(targets):
                if n > 0:
                    wait = random.uniform(cfg["delay_min_seconds"], cfg["delay_max_seconds"])
                    print(f"等待 {wait:.0f} 秒后打开下一篇…")
                    page.wait_for_timeout(wait * 1000)
                tag = f"{t['type']}_{t['id']}"
                rec.phase = media.phase = tag
                print(f"打开详情页：{t['title']}（{t['type']}）")
                goto(page, t["href"], "笔记详情页")
                human_pause(page, 5, 7)
                stop_if_blocked(page, rec)
                ex.snapshot(tag, html=True)
            print("\n✅ 详情页探索完成。")
        except StopSync as e:
            ex.note(f"\n⛔ 已停止：{e}")
            page.screenshot(path=str(out / "screenshot_stopped.png"))
            input("浏览器会保持打开，方便你查看。按回车键关闭…")
        except KeyboardInterrupt:
            ex.note("\n已手动中断。")
        except PlaywrightError as e:
            ex.note(f"\n⚠️ 浏览器出错或被关闭：{str(e).splitlines()[0]}")
        finally:
            ex.save_responses()
            (out / "media_urls.json").write_text(json.dumps(media.items, ensure_ascii=False, indent=1), "utf-8")
            (out / "summary.txt").write_text("\n".join(ex.notes), "utf-8")
            try:
                ctx.close()
            except PlaywrightError:
                pass
    print(f"\n结果目录：{out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StopSync as e:
        print(f"⛔ {e}")
        sys.exit(1)
