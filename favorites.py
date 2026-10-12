"""M2 收藏列表：打开「我」→「收藏」，慢慢滚动，拿到每篇笔记的 id、标题、详情链接。

数据来源（都来自 M1 探索的真实结果）：
- 列表顺序、标题：页面的 __INITIAL_STATE__.user.notes[收藏标签]，与收藏接口返回的顺序一致
- 详情链接：页面卡片 section.note-item[data-note-id] 里 a.cover 的 href（自带 xsec_token）

运行：.venv/bin/python favorites.py --show
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

from xhs_common import (
    DEBUG_DIR, HOME_URL, ResponseRecorder, StopSync, ensure_logged_in, first_page, goto,
    human_pause, load_config, open_browser, stop_if_blocked,
)

# 读取收藏标签的状态。页面是 Vue 写的，读取时顺手解开 ref。
_READ_FAV_STATE_JS = """
() => {
  const unref = v => (v && v.__v_isRef) ? v._value : v;
  const s = window.__INITIAL_STATE__;
  const u = s && unref(s.user);
  if (!u) return null;
  const tab = unref(u.activeTab) || {};
  const sub = unref(u.activeSubTab) || {};
  const notes = (unref(u.notes) || [])[tab.index] || [];
  const query = (unref(u.noteQueries) || [])[tab.index] || {};
  return {
    tab: tab.query, subTab: sub.query, total: sub.count, hasMore: query.hasMore,
    notes: notes.map(n => {
      const c = n.noteCard || {};
      return {id: n.id || c.noteId, title: c.displayTitle || '', type: c.type || '',
              author: (c.user && (c.user.nickname || c.user.nickName)) || ''};
    }),
  };
}
"""

# 读取当前渲染出来的卡片。列表是"虚拟滚动"的：滚过去的卡片会被移出页面，所以要边滚边记。
# 只看当前可见标签里的卡片（隐藏的「笔记」标签被压成了约 1 像素高）。
_READ_CARDS_JS = """
() => [...document.querySelectorAll('section.note-item[data-note-id]')]
  .filter(s => { const t = s.closest('.tab-content-item'); return !t || t.offsetHeight > 10; })
  .map(s => {
    const a = s.querySelector('a.cover');
    const t = s.querySelector('a.title');
    return {id: s.dataset.noteId, index: Number(s.dataset.index), href: a ? a.href : null,
            title: t ? (t.innerText || '').trim() : ''};
  })
"""


def read_fav_state(page: Page) -> dict | None:
    try:
        return page.evaluate(_READ_FAV_STATE_JS)
    except PlaywrightError:
        return None


def open_favorites(page: Page, rec: ResponseRecorder, interactive: bool) -> dict:
    """从首页出发：确认登录 → 打开我的主页 → 点「收藏」。返回收藏标签的状态。"""
    rec.phase = "home"
    print("打开小红书首页…")
    goto(page, HOME_URL, "小红书首页")
    human_pause(page, 3, 5)
    stop_if_blocked(page, rec, allow_login_page=True)
    profile_url = ensure_logged_in(page, rec, interactive=interactive)
    human_pause(page, 2, 4)

    rec.phase = "profile"
    print("打开我的主页…")
    goto(page, profile_url, "我的主页")
    human_pause(page, 3, 5)
    stop_if_blocked(page, rec)

    rec.phase = "fav"
    print("切换到「收藏」标签…")
    tabs = page.get_by_text("收藏", exact=True)
    for i in range(tabs.count()):
        if tabs.nth(i).is_visible():
            tabs.nth(i).click()
            break
    human_pause(page, 3, 5)
    stop_if_blocked(page, rec)
    state = read_fav_state(page)
    if not state or state["tab"] != "fav" or state["subTab"] != "note":
        raise StopSync(f"没能切换到「收藏 → 笔记」标签（当前页面状态：{state and (state['tab'], state['subTab'])}）。"
                       "可能是页面改版了，请把这句话发给我。")
    print(f"收藏里共有 {state['total']} 篇笔记。")
    return state


def collect_favorites(page: Page, rec: ResponseRecorder, limit: int,
                      known_ids: set[str] = frozenset(), stop_at_known: bool = True,
                      max_scrolls: int = 300) -> tuple[list[dict], dict]:
    """在已经打开的收藏标签上，从上往下慢慢滚动，按收藏顺序返回笔记。

    - 遇到 known_ids 里的 id：stop_at_known=True 时停止（增量同步），否则跳过继续往下找
    - 拿到 limit 篇新笔记（且都拿到了详情链接）就停止
    - 返回 (笔记列表, 扫描信息)
      笔记：{id, title, type, author, href, position}；href 为 None 表示没拿到链接
      扫描信息：{end_reached: 是否看完了整个收藏列表, seen_ids: 看到过的所有笔记 id, total: 收藏总数}
    """
    hrefs: dict[str, str] = {}
    max_index_seen = -1
    stale_rounds = 0
    last_progress = None
    page.mouse.move(640, 600)  # 鼠标放在列表区域，滚轮才会滚动页面

    for scroll in range(max_scrolls + 1):
        stop_if_blocked(page, rec)
        try:
            cards = page.evaluate(_READ_CARDS_JS)
        except PlaywrightError:
            cards = []
        for c in cards:
            if c["href"]:
                hrefs[c["id"]] = c["href"]
            max_index_seen = max(max_index_seen, c["index"])
        state = read_fav_state(page) or {"notes": [], "hasMore": True}

        # 按收藏顺序挑出这次要处理的笔记
        picked, hit_known, examined_all = [], False, True
        for pos, n in enumerate(state["notes"]):
            if n["id"] in known_ids:
                if stop_at_known:
                    hit_known = True
                    break
                continue
            picked.append({**n, "position": pos, "href": hrefs.get(n["id"])})
            if len(picked) >= limit:
                examined_all = pos == len(state["notes"]) - 1
                break
        end_of_list = not state["hasMore"]
        enough = len(picked) >= limit or hit_known or end_of_list
        # 已经滚过了这些卡片却仍没拿到链接的，就不再等了
        waiting = [p for p in picked if not p["href"] and p["position"] > max_index_seen - 5]
        if enough and not waiting:
            if hit_known:
                print(f"遇到已同步过的笔记，停止。本次找到 {len(picked)} 篇新笔记。")
            elif end_of_list and len(picked) < limit:
                print(f"已到收藏列表末尾。本次找到 {len(picked)} 篇。")
            break

        progress = (len(state["notes"]), len(hrefs), max_index_seen)
        stale_rounds = stale_rounds + 1 if progress == last_progress else 0
        last_progress = progress
        if stale_rounds >= 6:
            print("⚠️  连续滚动多次都没有新内容，停止滚动。")
            break
        if scroll == max_scrolls:
            print(f"⚠️  已滚动 {max_scrolls} 次，达到上限，停止。")
            break

        print(f"  已加载 {len(state['notes'])}/{state.get('total') or '?'} 篇，"
              f"拿到 {len(hrefs)} 个链接，继续向下滚动…")
        rec.phase = "fav_scroll"
        page.mouse.wheel(0, random.randint(600, 1000))
        human_pause(page, 2.5, 5)

    for p in picked:
        if not p["title"]:
            p["title"] = "（无标题）"
    scan = {
        "end_reached": end_of_list and examined_all and not hit_known and not waiting,
        "seen_ids": [n["id"] for n in state["notes"]],
        "total": state.get("total"),
    }
    return picked, scan


def main() -> int:
    parser = argparse.ArgumentParser(description="列出小红书收藏（M2 测试用，不会写入 Obsidian）")
    parser.add_argument("--show", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--limit", type=int, help="最多列出多少篇（默认用 config.toml 里的 max_notes_per_run）")
    parser.add_argument("--stop-at", metavar="笔记ID", help="模拟增量同步：假装这篇已经同步过，遇到它就停止")
    args = parser.parse_args()

    cfg = load_config()
    limit = args.limit or cfg["max_notes_per_run"]
    headless = cfg["headless"] and not args.show
    known = {args.stop_at} if args.stop_at else set()

    with sync_playwright() as pw:
        ctx = open_browser(pw, cfg, headless=headless)
        page = first_page(ctx)
        rec = ResponseRecorder(page)
        try:
            open_favorites(page, rec, interactive=not headless)
            items, _ = collect_favorites(page, rec, limit, known_ids=known)
        except StopSync as e:
            print(f"\n⛔ 已停止：{e}")
            if not headless:
                input("浏览器会保持打开，方便你查看。按回车键关闭…")
            return 1
        except PlaywrightError as e:
            print(f"\n⚠️ 浏览器出错或被关闭：{str(e).splitlines()[0]}")
            return 1
        finally:
            try:
                ctx.close()
            except PlaywrightError:
                pass

    print()
    for i, it in enumerate(items, 1):
        mark = "✓" if it["href"] else "✗ 没拿到链接"
        print(f"{i:3d}. [{it['id']}] {it['title'][:30]}  —— {it['author']}  {mark}")
    DEBUG_DIR.mkdir(exist_ok=True)
    out = DEBUG_DIR / datetime.now().strftime("favorites-%Y%m%d-%H%M%S.json")
    out.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n共 {len(items)} 篇，完整结果（含链接）已保存到：{out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StopSync as e:
        print(f"⛔ {e}")
        sys.exit(1)
