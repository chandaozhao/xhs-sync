"""xhs-sync 公共模块：配置、浏览器、登录检测、安全检查、被动记录响应。

原则：只读取浏览器正常加载页面得到的数据，不自己构造任何接口请求。
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # Python 3.9/3.10

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.toml"
DEBUG_DIR = PROJECT_DIR / "debug"
HOME_URL = "https://www.xiaohongshu.com/explore"

DEFAULTS = {
    "vault_path": "",
    "root_dir": "RedNote",
    "max_notes_per_run": 20,
    "delay_min_seconds": 20,
    "delay_max_seconds": 40,
    "video_quality": "lowest",
    "auto_clean_media": True,
    "profile_dir": "~/.xhs-sync/profile",
    "headless": True,
    "auto_transcribe": True,
    "transcribe_language": "zh",
    "whisper_model": "~/.xhs-sync/models/ggml-large-v3-turbo.bin",
}

# 出现在 URL 路径里就说明被带去了验证页
BLOCK_URL_WORDS = ("captcha", "verify")
# 出现在弹窗/浮层里，或者整页只剩一小段提示文字时，视为被拦截
BLOCK_TEXT_WORDS = (
    "访问频繁", "操作频繁", "请求频繁", "请求太频繁", "频繁访问",
    "安全验证", "滑块", "请稍后再试", "账号异常", "环境异常", "存在风险",
)
# 接口返回的错误信息里出现这些词，视为被拦截
BLOCK_API_WORDS = ("频繁", "验证", "风险", "风控", "captcha")


class StopSync(Exception):
    """遇到验证码、访问频繁、登录失效等情况：立刻停止，交给人处理。"""


class ProfileBusy(StopSync):
    """浏览器资料目录正被占用（比如正在手动运行）。定时同步遇到时只跳过这一次。"""


class NetworkTrouble(StopSync):
    """网络或代理不稳定，打不开页面。定时同步遇到时只跳过这一次，不暂停。"""


# ---------------------------------------------------------------- 配置

def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise SystemExit("⛔ 还没有配置文件：把 config.example.toml 复制一份改名为 config.toml，"
                         "填写里面的 vault_path（运行 bash setup.sh 会自动复制）。")
    with open(path, "rb") as f:
        cfg = {**DEFAULTS, **tomllib.load(f)}
    if not str(cfg["vault_path"]).strip():
        raise SystemExit("⛔ 请先在 config.toml 里填写 vault_path（你的 Obsidian Vault 路径）。")
    cfg["profile_dir"] = Path(cfg["profile_dir"]).expanduser()
    cfg["vault_path"] = Path(cfg["vault_path"]).expanduser()
    cfg["whisper_model"] = Path(cfg["whisper_model"]).expanduser()
    if cfg["delay_min_seconds"] > cfg["delay_max_seconds"]:
        raise SystemExit("config.toml 里 delay_min_seconds 不能大于 delay_max_seconds")
    if cfg["video_quality"] not in ("lowest", "highest"):
        raise SystemExit('config.toml 里 video_quality 只能是 "lowest" 或 "highest"')
    return cfg


def vault_access_error(cfg: dict) -> str | None:
    """检查能不能真正读到 Vault。返回错误说明，正常返回 None。

    定时任务（launchd）里的程序默认没有权限读 iCloud 云盘：目录能看到，但列不出内容，
    glob 之类会安静地返回「空」——这时继续同步会写出重复笔记，所以必须先查出来。
    """
    vault, root = cfg["vault_path"], cfg["vault_path"] / cfg["root_dir"]
    try:
        os.listdir(vault)
        if root.is_dir():
            os.listdir(root)
    except OSError as e:
        return f"读不了 Obsidian Vault（{e.strerror}）"
    return None


# ---------------------------------------------------------------- 浏览器

def open_browser(pw, cfg: dict, headless: bool) -> BrowserContext:
    """打开带持久资料目录的浏览器，登录状态保存在 profile_dir 里。"""
    profile = cfg["profile_dir"]
    profile.mkdir(parents=True, exist_ok=True)
    try:
        return pw.chromium.launch_persistent_context(
            str(profile),
            channel="chromium",  # 有界面/无界面都用同一个完整版 Chromium，共用登录资料
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
    except PlaywrightError as e:
        if "Singleton" in str(e) or "already in use" in str(e):
            raise ProfileBusy("浏览器资料目录正被占用：上一次打开的浏览器可能还没关，请先关掉再运行。") from e
        raise


def first_page(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def human_pause(page: Page, low: float, high: float) -> None:
    page.wait_for_timeout(random.uniform(low, high) * 1000)


def goto(page: Page, url: str, what: str = "页面") -> None:
    """打开网页；网络卡住超时就等一会儿再试一次，还不行就停下。"""
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except PlaywrightTimeoutError:
            if attempt == 2:
                raise NetworkTrouble(f"打开{what}超时（等了 60 秒）。多半是网络或代理不稳定，"
                               "请确认平时的浏览器能正常打开小红书后再试。")
            print(f"  打开{what}超时，15 秒后重试一次…")
            page.wait_for_timeout(15000)


# ---------------------------------------------------------------- 被动记录响应

class ResponseRecorder:
    """监听页面自己发出的请求的响应，只读取，不发起任何请求。

    同时顺带检查接口是否返回了"需要验证/访问频繁/未登录"之类的信号。
    """

    def __init__(self, page: Page, keep: bool = True):
        self.keep = keep  # False：只做拦截检查，不保存响应（日常同步用，省内存）
        self.records: list[dict] = []
        self.phase = "start"
        self.block_reason: str | None = None
        self.login_reason: str | None = None
        page.on("response", self._on_response)

    def _on_response(self, resp: Response) -> None:
        try:
            host = urlparse(resp.url).hostname or ""
            if not host.endswith("xiaohongshu.com"):
                return
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            path = urlparse(resp.url).path
            rec = {
                "phase": self.phase,
                "url": resp.url,
                "path": path,
                "method": resp.request.method,
                "status": resp.status,
                "content_type": resp.headers.get("content-type", ""),
                "time": time.time(),
            }
            if resp.status in (461, 471):
                self.block_reason = f"接口 {path} 返回 HTTP {resp.status}（通常表示需要人工验证）"
            if "json" in rec["content_type"]:
                try:
                    rec["body"] = resp.json()
                except Exception as e:  # 有些响应体拿不到（比如页面已跳走）
                    rec["body_error"] = str(e)
                else:
                    self._check_api_body(path, rec["body"])
            if self.keep:
                self.records.append(rec)
        except Exception:
            pass  # 记录失败不能影响主流程

    def _check_api_body(self, path: str, body) -> None:
        if "/api/" not in path or not isinstance(body, dict):
            return
        msgs = " ".join(str(v) for v in body.values() if isinstance(v, str))
        if not msgs:
            return
        if any(w in msgs for w in BLOCK_API_WORDS):
            self.block_reason = f"接口 {path} 返回：{msgs[:100]}"
        elif "登录" in msgs:
            self.login_reason = f"接口 {path} 返回：{msgs[:100]}"


# ---------------------------------------------------------------- 安全检查

# 笔记详情是一个浮层；它里面的标题、正文、评论、作者是用户写的内容，不能当成拦截提示来扫描
USER_CONTENT_SELECTOR = ".note-scroller, #detail-title, #detail-desc, .comments-container, .author-wrapper"

_SCAN_OVERLAYS_JS = """
([words, skipSel]) => {
  const hits = [];
  const scan = (text, where) => {
    for (const w of words) {
      const i = text.indexOf(w);
      if (i >= 0) hits.push({word: w, where, snippet: text.slice(Math.max(0, i - 30), i + 30)});
    }
  };
  const textOf = (el) => {  // 元素里的文字，但跳过用户内容区域
    const parts = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const p = n.parentElement;
      if (p && !p.closest(skipSel)) parts.push(n.nodeValue);
    }
    return parts.join(' ');
  };
  const body = document.body ? document.body.innerText : '';
  // 整页文字很少，多半是错误/拦截页
  if (body.trim().length < 800) scan(body, 'page');
  // 只看固定定位的浮层（弹窗、提示条），避免把笔记正文里的字误判为拦截
  for (const el of document.querySelectorAll('body *')) {
    const st = getComputedStyle(el);
    if (st.position !== 'fixed' || st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 20) continue;
    scan(textOf(el), 'overlay');
  }
  return hits;
}
"""


def check_blocked(page: Page, rec: ResponseRecorder | None = None, allow_login_page: bool = False) -> str | None:
    """返回拦截原因（字符串），没有问题则返回 None。"""
    parsed = urlparse(page.url)
    path = parsed.path.lower()
    if "website-login/error" in path:
        msg = parse_qs(parsed.query).get("error_msg", ["（无说明）"])[0]
        code = parse_qs(parsed.query).get("error_code", ["?"])[0]
        return f"小红书拒绝了访问：{msg}（错误码 {code}）"
    if any(w in path for w in BLOCK_URL_WORDS):
        return f"页面跳转到了验证页：{page.url}"
    for frame in page.frames:
        if "captcha" in urlparse(frame.url).path.lower():
            return f"页面里出现了验证码：{frame.url}"
    if not allow_login_page and "login" in path:
        return f"页面跳转到了登录页：{page.url}"
    if rec and rec.block_reason:
        return rec.block_reason
    if rec and rec.login_reason and not allow_login_page:
        return f"登录状态可能已失效。{rec.login_reason}"
    if allow_login_page:
        return None  # 登录弹窗里的文字（"二维码已过期，请稍后再试"等）不算拦截
    try:
        hits = page.evaluate(_SCAN_OVERLAYS_JS, [list(BLOCK_TEXT_WORDS), USER_CONTENT_SELECTOR])
    except PlaywrightError:
        return None  # 页面正在跳转，下次再查
    if hits:
        h = hits[0]
        return f"页面出现提示「{h['word']}」：…{h['snippet'].strip()}…"
    return None


def stop_if_blocked(page: Page, rec: ResponseRecorder | None = None, allow_login_page: bool = False) -> None:
    reason = check_blocked(page, rec, allow_login_page)
    if reason:
        raise StopSync(reason)


# ---------------------------------------------------------------- 登录检测

_LOGIN_INFO_JS = """
() => {
  const links = [...document.querySelectorAll('a[href*="/user/profile/"]')];
  const me = links.find(a => (a.innerText || '').trim() === '我');
  return {profileUrl: me ? me.href : null};
}
"""


def get_profile_url(page: Page) -> str | None:
    """已登录时，左侧栏有一个文字为「我」、指向 /user/profile/<你的id> 的链接。"""
    try:
        return page.evaluate(_LOGIN_INFO_JS)["profileUrl"]
    except PlaywrightError:
        return None  # 页面正在跳转


def ensure_logged_in(page: Page, rec: ResponseRecorder, interactive: bool, timeout_s: int = 300) -> str:
    """确认已登录，返回「我的主页」链接。

    interactive=True（有界面）时，未登录会提示扫码并等待；否则直接报错。
    """
    profile_url = get_profile_url(page)
    if profile_url:
        return profile_url
    if not interactive:
        raise StopSync("当前未登录（或登录已过期）。请先用有界面模式运行一次并扫码登录。")

    print()
    print("=" * 60)
    print("  还没有登录。请看弹出的浏览器窗口：")
    print("  用手机小红书 App 扫描页面上的二维码，并在手机上确认登录。")
    print(f"  脚本会自动等待（最多 {timeout_s // 60} 分钟）。")
    print("  如果页面上没有二维码，请点击左侧的「登录」按钮。")
    print("=" * 60)

    deadline = time.time() + timeout_s
    last_hint = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(3000)
        stop_if_blocked(page, rec, allow_login_page=True)
        profile_url = get_profile_url(page)
        if profile_url:
            print("✅ 登录成功！")
            rec.login_reason = None  # 登录前接口返回的"未登录"不再算数
            return profile_url
        # 兜底：如果识别不到「我」，你也可以手动点左侧「我」进入自己的主页
        if "/user/profile/" in page.url:
            print("✅ 检测到你已进入个人主页，视为已登录。")
            rec.login_reason = None
            return page.url
        if time.time() - last_hint > 30:
            print("  …仍在等待扫码登录。若已登录但没反应，请在浏览器里点左侧「我」。")
            last_hint = time.time()
    raise StopSync("等待登录超时。请重新运行并在 5 分钟内完成扫码。")
