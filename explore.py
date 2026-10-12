"""M1 探索脚本：登录 → 打开「我」的主页 → 点「收藏」→ 慢慢滚动几下，
把 __INITIAL_STATE__ 结构、页面自己请求到的 JSON 响应、链接和截图存到 debug/。

运行：.venv/bin/python explore.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from xhs_common import (
    DEBUG_DIR, HOME_URL, ResponseRecorder, StopSync, ensure_logged_in, first_page, goto,
    human_pause, load_config, open_browser, stop_if_blocked,
)

# 安全地把页面里的 __INITIAL_STATE__ 转成 JSON：
# 解开 Vue 的 ref（__v_isRef），跳过函数，防止循环引用，限制深度。
_DUMP_STATE_JS = """
() => {
  const MAX_DEPTH = 16;
  const stack = new WeakSet();
  function walk(v, depth) {
    if (v === null || v === undefined) return null;
    const t = typeof v;
    if (t === 'string' || t === 'number' || t === 'boolean') return v;
    if (t === 'bigint') return v.toString();
    if (t !== 'object') return undefined;
    if (depth > MAX_DEPTH) return '[MaxDepth]';
    try { if (v.__v_isRef) return walk(v._rawValue !== undefined ? v._rawValue : v._value, depth + 1); } catch (e) {}
    if (typeof Node !== 'undefined' && v instanceof Node) return '[DOM Node]';
    if (stack.has(v)) return '[Circular]';
    stack.add(v);
    let out;
    if (Array.isArray(v)) {
      out = v.map(x => walk(x, depth + 1));
    } else if (v instanceof Map) {
      out = {};
      v.forEach((val, key) => { out[String(key)] = walk(val, depth + 1); });
    } else {
      out = {};
      for (const k of Object.keys(v)) {
        if (k.startsWith('__v_')) continue;
        let x; try { x = v[k]; } catch (e) { continue; }
        const w = walk(x, depth + 1);
        if (w !== undefined) out[k] = w;
      }
    }
    stack.delete(v);
    return out;
  }
  const candidates = Object.keys(window).filter(k => /STATE|INITIAL|SSR|NUXT|NEXT_DATA/.test(k));
  const result = {_windowKeys: candidates};
  for (const k of candidates) {
    try { result[k] = walk(window[k], 0); } catch (e) { result[k] = '[Error] ' + e; }
  }
  return result;
}
"""

_LINKS_JS = """
() => [...document.querySelectorAll('a[href]')]
  .filter(a => /xsec_token|\\/explore\\/|\\/discovery\\/item\\/|\\/user\\/profile\\/[^/?#]+\\/[^/?#]+/.test(a.getAttribute('href')))
  .map(a => {
    const card = a.closest('section') || a.parentElement;
    return {
      href: a.href,
      rawHref: a.getAttribute('href'),
      text: (a.innerText || '').trim().slice(0, 80),
      cls: a.className,
      visible: a.getBoundingClientRect().height > 0,
      cardTag: card ? card.tagName : null,
      cardCls: card ? String(card.className) : null,
      cardText: card ? (card.innerText || '').trim().slice(0, 120) : '',
    };
  })
"""

_CLICK_TAB_JS_INFO = """
(el) => ({tag: el.tagName, cls: String(el.className), parentCls: el.parentElement ? String(el.parentElement.className) : null,
          html: el.outerHTML.slice(0, 300)})
"""


def outline(obj, path: str = "", depth: int = 0, lines: list | None = None, max_depth: int = 14) -> list:
    """把一个 JSON 结构概括成 "路径: 类型 = 示例值" 的文本，方便人读。"""
    if lines is None:
        lines = []
    name = path or "<root>"
    if depth > max_depth:
        lines.append(f"{name}: …")
        return lines
    if isinstance(obj, dict):
        lines.append(f"{name}: dict({len(obj)})")
        items = list(obj.items())
        for k, v in items[:40]:
            outline(v, f"{path}.{k}" if path else str(k), depth + 1, lines)
        if len(items) > 40:
            lines.append(f"{name}.<另外 {len(items) - 40} 个键>")
    elif isinstance(obj, list):
        lines.append(f"{name}: list[{len(obj)}]")
        for i, v in enumerate(obj[:5] if len(obj) <= 5 else obj[:1]):
            outline(v, f"{path}[{i}]", depth + 1, lines)
    else:
        s = json.dumps(obj, ensure_ascii=False)
        if len(s) > 80:
            s = s[:80] + "…"
        lines.append(f"{name}: {type(obj).__name__} = {s}")
    return lines


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60]


class Explorer:
    def __init__(self, page, rec: ResponseRecorder, out: Path):
        self.page = page
        self.rec = rec
        self.out = out
        self.notes: list[str] = []  # 写进 summary.txt 的要点

    def note(self, msg: str) -> None:
        print(msg)
        self.notes.append(msg)

    def snapshot(self, tag: str, html: bool = False, links: bool = False) -> None:
        page = self.page
        try:
            state = page.evaluate(_DUMP_STATE_JS)
        except Exception as e:
            state = {"_error": str(e)}
        (self.out / f"state_{tag}.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
        (self.out / f"state_{tag}_outline.txt").write_text("\n".join(outline(state)), "utf-8")
        page.screenshot(path=str(self.out / f"screenshot_{tag}.png"))
        if html:
            (self.out / f"page_{tag}.html").write_text(page.content(), "utf-8")
        if links:
            data = page.evaluate(_LINKS_JS)
            (self.out / f"links_{tag}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            self.note(f"  [{tag}] 页面上找到 {len(data)} 个笔记相关链接")
        self.note(f"  [{tag}] URL = {page.url}；window 里的状态变量：{state.get('_windowKeys')}")

    def click_tab(self, text: str) -> bool:
        loc = self.page.get_by_text(text, exact=True)
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                info = el.evaluate(_CLICK_TAB_JS_INFO)
                self.note(f"  点击了「{text}」元素：{json.dumps(info, ensure_ascii=False)}")
                el.click()
                return True
        return False

    def save_responses(self) -> None:
        resp_dir = self.out / "responses"
        resp_dir.mkdir(exist_ok=True)
        index, outlines = [], []
        for i, r in enumerate(self.rec.records):
            entry = {k: v for k, v in r.items() if k != "body"}
            if "body" in r:
                fname = f"{i:03d}_{r['phase']}_{slug(r['path'])}.json"
                (resp_dir / fname).write_text(json.dumps(r, ensure_ascii=False, indent=1), "utf-8")
                entry["file"] = fname
                body = r["body"]
                entry["top_keys"] = list(body.keys()) if isinstance(body, dict) else type(body).__name__
                outlines.append(f"\n##### {fname}\n{r['method']} {r['status']} {r['url'][:200]}")
                outlines.extend(outline(body)[:80])
            index.append(entry)
        (self.out / "responses_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
        (self.out / "responses_outline.txt").write_text("\n".join(outlines), "utf-8")
        json_count = sum(1 for e in index if "file" in e)
        self.note(f"  共记录 {len(index)} 个接口响应，其中 JSON {json_count} 个")


def main() -> int:
    cfg = load_config()
    out = DEBUG_DIR / datetime.now().strftime("explore-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"探索结果会保存到：{out}")

    with sync_playwright() as pw:
        ctx = open_browser(pw, cfg, headless=False)  # 探索脚本总是有界面，方便扫码和观察
        page = first_page(ctx)
        rec = ResponseRecorder(page)
        ex = Explorer(page, rec, out)
        try:
            # 1. 首页 + 登录
            rec.phase = "home"
            print("打开小红书首页…")
            goto(page, HOME_URL, "小红书首页")
            human_pause(page, 3, 5)
            stop_if_blocked(page, rec, allow_login_page=True)
            profile_url = ensure_logged_in(page, rec, interactive=True)
            human_pause(page, 2, 4)
            ex.note(f"我的主页：{profile_url}")
            ex.snapshot("home")

            # 2. 我的主页
            rec.phase = "profile"
            print("打开我的主页…")
            goto(page, profile_url, "我的主页")
            human_pause(page, 4, 6)
            stop_if_blocked(page, rec)
            ex.snapshot("profile", html=True, links=True)

            # 3. 收藏标签
            rec.phase = "fav"
            print("切换到「收藏」标签…")
            if not ex.click_tab("收藏"):
                print("⚠️  没找到「收藏」标签，请在浏览器里手动点一下「收藏」，脚本 20 秒后继续。")
                page.wait_for_timeout(20000)
            human_pause(page, 4, 6)
            stop_if_blocked(page, rec)
            ex.snapshot("fav", html=True, links=True)

            # 4. 慢慢向下滚动几次，触发页面自己加载更多
            rec.phase = "fav_scroll"
            for i in range(3):
                print(f"向下滚动（{i + 1}/3）…")
                page.mouse.wheel(0, 900)
                human_pause(page, 3, 5)
                stop_if_blocked(page, rec)
            ex.snapshot("fav_scrolled", html=True, links=True)
            print("\n✅ 探索完成。")
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
    except StopSync as e:  # 比如浏览器资料目录被占用
        print(f"⛔ {e}")
        sys.exit(1)
