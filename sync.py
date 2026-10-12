"""xhs-sync 主程序：把小红书收藏同步到 Obsidian。

流程：打开「我」→「收藏」→ 按收藏顺序找出要同步的笔记（增量，见 state.py）
     → 逐篇用卡片链接打开详情页 → 下载图片/视频 → 写 Markdown → 记入 state.json
     → 清理已删除笔记的媒体 → 把新视频转成文字稿（transcribe.py）

运行：.venv/bin/python sync.py            （按 config.toml 的 headless 设置）
     .venv/bin/python sync.py --show     （显示浏览器窗口；登录过期需要扫码时用这个）
     定时同步（Claude 桌面 App 的定时任务）用的是 sync.py --scheduled，规则见 autorun.py
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError, sync_playwright

import autorun
from favorites import collect_favorites, open_favorites
from media import download_media
from note_detail import NoteError, parse_note, plan_media, read_note_raw, render_markdown, write_note
from cleanup import auto_clean, find_orphan_media
from state import SyncState, vault_note_ids
from transcribe import transcribe_after_sync
from xhs_common import (
    ProfileBusy, ResponseRecorder, StopSync, first_page, goto, human_pause, load_config, open_browser,
    stop_if_blocked, vault_access_error,
)


def polite_wait(page, cfg: dict) -> None:
    """两篇详情页之间随机等待（默认 20–40 秒）。"""
    wait = random.uniform(cfg["delay_min_seconds"], cfg["delay_max_seconds"])
    print(f"  等待 {wait:.0f} 秒…")
    page.wait_for_timeout(wait * 1000)


def sync_note(page, rec: ResponseRecorder, cfg: dict, item: dict):
    """打开一篇笔记的详情页（用卡片上的 href），下载媒体，写成 Markdown。返回 (文件路径, 媒体数)。"""
    rec.phase = f"detail_{item['id']}"
    goto(page, item["href"], "笔记详情页")
    human_pause(page, 3, 6)
    stop_if_blocked(page, rec)
    note = parse_note(read_note_raw(page, item["id"]), item["href"], cfg["video_quality"])
    if note["video"]:
        v = note["video"]
        size = f"约 {v['size'] / 1e6:.1f} MB" if v["size"] else "大小未知"
        print(f"  视频：{v['width']}×{v['height']}，{size}（{v['options']} 种带声音的清晰度可选）")
    root = cfg["vault_path"] / cfg["root_dir"]
    # 媒体地址有有效期，打开详情页后立刻下载
    media, errors = download_media(page.context, plan_media(note), root / "Media" / note["id"])
    markdown = render_markdown(
        note, media,
        synced_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        media_rel_dir=f"../Media/{note['id']}",
    )
    path = write_note(root / "Bookmarks", note, markdown)
    if errors:  # 文字已保存，但媒体不完整：算失败，下次重试时会补上
        raise NoteError(f"已写入 {path.name}，但有 {len(errors)} 个媒体下载失败：{'；'.join(errors[:3])}", file=path.name)
    return path, len(media)


def main() -> int:
    parser = argparse.ArgumentParser(description="把小红书收藏同步到 Obsidian")
    parser.add_argument("--show", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--limit", type=int, help="本次最多同步多少篇（默认用 config.toml 的 max_notes_per_run）")
    parser.add_argument("--scheduled", action="store_true", help="定时任务用（见 autorun.py）")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg["vault_path"].is_dir():
        print(f"⛔ 找不到 Obsidian Vault：{cfg['vault_path']}\n请检查 config.toml 里的 vault_path。")
        return 1
    err = vault_access_error(cfg)
    if err:
        print(f"⛔ {err}。为了不写出重复笔记，这次不同步。")
        if args.scheduled:
            autorun.notify(f"定时同步{err}，这次没有同步。")
        return 1
    limit = args.limit or cfg["max_notes_per_run"]
    headless = cfg["headless"] and not args.show
    root = cfg["vault_path"] / cfg["root_dir"]
    if args.scheduled:
        peek = SyncState.load(root)
        if not autorun.before_scheduled_run(backfill_done=peek.stop_at_known()):
            return 0
    state = SyncState.load(root)  # 定时同步随机等待之后再读，期间你手动同步过也不会丢进度
    backfill_was_done = state.backfill_done
    deleted = state.skip_deleted(vault_note_ids(cfg["vault_path"]))
    if deleted:
        print(f"有 {len(deleted)} 篇待重试的笔记已在 Obsidian 里删除，不再重试：{'、'.join(deleted[:3])}")
        state.save()
    print(f"同步记录：已同步 {len(state.synced)} 篇，待重试 {len(state.failed)} 篇，已放弃 {len(state.gave_up)} 篇。")
    print(f"本次模式：{state.mode_text()}；最多 {limit} 篇。")
    done, failed, total = 0, 0, None
    interrupted, ok, stopped, ctx = False, False, None, None

    with sync_playwright() as pw:
        try:
            ctx = open_browser(pw, cfg, headless=headless)
            page = first_page(ctx)
            rec = ResponseRecorder(page, keep=False)
            open_favorites(page, rec, interactive=not headless)
            items, scan = collect_favorites(page, rec, limit, known_ids=state.known_ids(),
                                            stop_at_known=state.stop_at_known())
            total = scan["total"]
            state.after_scan(scan)
            state.save()
            print(f"\n本次要同步 {len(items)} 篇。")
            for n, item in enumerate(items, 1):
                if n > 1:
                    polite_wait(page, cfg)
                retry = "（重试）" if item["id"] in state.failed else ""
                print(f"[{n}/{len(items)}] {item['title']}{retry}")
                try:
                    if not item["href"]:
                        raise NoteError("没拿到详情链接")
                    path, n_media = sync_note(page, rec, cfg, item)
                except NoteError as e:
                    gave_up = state.mark_failed(item, str(e), e.file)
                    state.save()
                    failed += 1
                    print(f"  ✗ {e}" + ("（已失败 3 次，放弃这篇）" if gave_up else "（下次重试）"))
                    continue
                state.mark_synced(item, path.name)
                state.save()  # 每篇都立刻保存，中途停止也不会丢进度
                done += 1
                print(f"  ✓ 已写入 {path.relative_to(cfg['vault_path'])}（{n_media} 个图片/视频）")
            ok = True
        except StopSync as e:
            stopped = e
            print(f"\n⛔ 已停止：{e}")
            if not headless and ctx and not args.scheduled:
                input("浏览器会保持打开，方便你查看。按回车键关闭…")
        except KeyboardInterrupt:
            interrupted = True
            print("\n已手动中断（已完成的进度都已保存）。")
        except PlaywrightError as e:
            print(f"\n⚠️ 浏览器出错或被关闭：{str(e).splitlines()[0]}")
        finally:
            try:
                if ctx:
                    ctx.close()
            except PlaywrightError:
                pass

    if args.scheduled:
        autorun.after_scheduled_run(stopped, ok, state.backfill_done and not backfill_was_done)
    elif ok:
        autorun.resume_if_paused()
    if isinstance(stopped, ProfileBusy):
        # 另一次同步正在下载媒体：这时清理会把它还没写笔记的媒体当成「已删除笔记」，所以直接结束
        return 1

    print(f"\n本次：成功 {done} 篇，失败 {failed} 篇。")
    print(f"累计：已同步 {len(state.synced)}{f'/{total}' if total else ''} 篇，"
          f"待重试 {len(state.failed)} 篇，已放弃 {len(state.gave_up)} 篇。")
    if state.failed:
        for info in list(state.failed.values())[:5]:
            print(f"  待重试：{info['title']} —— {info['error']}")
    if cfg["auto_clean_media"]:
        auto_clean(cfg["vault_path"], root, state)
    else:
        orphans = find_orphan_media(root / "Media", vault_note_ids(cfg["vault_path"]), state)
        if orphans:
            size = sum(o["bytes"] for o in orphans) / 1e6
            print(f"\n提示：有 {len(orphans)} 篇笔记已在 Obsidian 里删除，它们的图片/视频还占着 {size:.0f} MB。")
            print("      运行 .venv/bin/python cleanup.py 可以把它们移到废纸篓。")
    if cfg["auto_transcribe"] and not interrupted:
        transcribe_after_sync(cfg)
    return 0 if not failed else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StopSync as e:
        print(f"⛔ {e}")
        sys.exit(1)
