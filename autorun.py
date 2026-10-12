"""定时自动同步的规则（由 Claude 桌面 App 的定时任务每天运行 sync.py --scheduled）。

为了不被风控：
- 每天 4 次（约 9、13、17、21 点），每次再随机推迟 0–50 分钟，每次最多 20 篇
- 旧收藏补齐后，每天只同步 1 次（其余几次直接跳过）
- 遇到验证码、访问频繁、登录失效等：暂停之后所有定时同步（写 paused.txt），弹通知告诉你；
  你手动运行一次 sync.py（需要扫码就加 --show）并成功后，自动恢复
- 网络没连上：只跳过这一次并通知；你正在手动运行时：这次定时同步自动跳过

看状态：.venv/bin/python autorun.py
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from datetime import datetime

from xhs_common import PROJECT_DIR, NetworkTrouble, ProfileBusy, StopSync

LAST_RUN = PROJECT_DIR / "last-scheduled-run.txt"  # 最近一次定时同步的日期
PAUSE_FILE = PROJECT_DIR / "paused.txt"  # 存在就表示定时同步已暂停，里面写着原因
MAX_DELAY_MINUTES = 50


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def notify(message: str, title: str = "小红书同步") -> None:
    """Mac 右上角弹通知。用 argv 传文字，不用担心引号转义。"""
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                    title, message], capture_output=True)


# ---------------------------------------------------------------- 暂停 / 恢复

def paused_reason() -> str | None:
    try:
        return PAUSE_FILE.read_text("utf-8").strip() or "（原因未知）"
    except OSError:
        return None


def pause(reason: str) -> None:
    PAUSE_FILE.write_text(f"{_now()} {reason}\n", "utf-8")


def resume_if_paused() -> None:
    """手动运行同步成功后调用：解除暂停。"""
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        print("\n▶️ 已恢复定时同步（之前因为异常暂停了）。")


# ---------------------------------------------------------------- 给 sync.py --scheduled 用

def before_scheduled_run(backfill_done: bool) -> bool:
    """定时同步开始前：检查是否暂停、今天是否已经同步过，然后随机等一会儿。返回 False 表示这次跳过。"""
    print(f"===== {_now()} 定时同步 =====")
    reason = paused_reason()
    if reason:
        print(f"定时同步已暂停，跳过：{reason}")
        print("处理好后手动运行一次 .venv/bin/python sync.py --show，成功后会自动恢复。")
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    if backfill_done and LAST_RUN.exists() and LAST_RUN.read_text("utf-8").strip() == today:
        print("旧收藏已补齐，每天只同步 1 次，今天已经同步过了，跳过。")
        return False
    delay = random.uniform(0, MAX_DELAY_MINUTES * 60)
    print(f"随机等待 {delay / 60:.0f} 分钟后开始…")
    time.sleep(delay)
    return True


def after_scheduled_run(stopped: StopSync | None, ok: bool, backfill_just_done: bool) -> None:
    """定时同步结束后：记录日期；被拦截就暂停并通知；旧收藏刚补齐也通知一声。"""
    if ok:
        LAST_RUN.write_text(datetime.now().strftime("%Y-%m-%d") + "\n", "utf-8")
    if isinstance(stopped, ProfileBusy):
        print("浏览器正在被另一次同步使用（可能你在手动运行），这次定时同步跳过。")
    elif isinstance(stopped, NetworkTrouble):
        notify("这次没连上小红书（网络或代理不稳定），下次定时会自动再试。")
    elif stopped:
        pause(str(stopped))
        print("⏸ 已暂停之后的定时同步。处理方法：在终端运行 .venv/bin/python sync.py --show，成功后自动恢复。")
        notify(f"已暂停自动同步：{str(stopped)[:60]}。处理方法：在终端运行 sync.py --show")
    if backfill_just_done:
        notify("旧收藏已经全部补齐！以后每天只同步 1 次新收藏。")


# ---------------------------------------------------------------- 状态

def main() -> int:
    reason = paused_reason()
    print(f"定时同步：{'⏸ 已暂停：' + reason if reason else '▶️ 正常'}")
    if reason:
        print("   处理好后手动运行一次 .venv/bin/python sync.py --show，成功后会自动恢复。")
    if LAST_RUN.exists():
        print(f"最近一次定时同步：{LAST_RUN.read_text('utf-8').strip()}")
    try:
        data = json.loads((PROJECT_DIR / "state.json").read_text("utf-8"))
        print(f"进度：已同步 {len(data.get('synced', {}))} 篇，待重试 {len(data.get('failed', {}))} 篇，"
              f"旧收藏{'已补齐' if data.get('backfillDone') else '还没补齐'}。")
    except (OSError, ValueError):
        pass
    print("每次运行的详细输出：Claude App 里「定时任务」的运行记录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
