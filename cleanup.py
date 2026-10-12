"""清理已删除笔记的图片/视频。

你在 Obsidian 里删掉笔记时，Obsidian 不会删它引用的图片和视频。
这里找出 RedNote/Media/ 里「整个 Vault 都没有对应笔记」的文件夹，移到废纸篓（可以「放回原处」）。

- 自动：每次 sync.py 结束时自动清理（config.toml 的 auto_clean_media）
- 手动：.venv/bin/python cleanup.py（会先列出来让你确认）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from state import SyncState, vault_note_ids
from xhs_common import load_config

_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def find_orphan_media(media_root: Path, present_ids: set[str], state: SyncState) -> list[dict]:
    """没有对应笔记的媒体文件夹。待重试、但笔记还没写进 Obsidian 的不算（重试时还要用）。"""
    if not media_root.is_dir():
        return []
    keep = {i for i, info in state.failed.items() if not info.get("file")}
    orphans = []
    for d in sorted(media_root.iterdir()):
        if not d.is_dir() or not _ID_RE.match(d.name) or d.name in present_ids or d.name in keep:
            continue
        info = state.synced.get(d.name) or state.gave_up.get(d.name) or state.failed.get(d.name) or {}
        orphans.append({
            "id": d.name, "path": d,
            "title": info.get("title") or "（标题未知）",
            "bytes": sum(f.stat().st_size for f in d.rglob("*") if f.is_file()),
        })
    return orphans


def icloud_pending(vault: Path) -> int:
    """还没从 iCloud 下载到本机的笔记数（Finder 里显示云朵图标，磁盘上是 .xxx.md.icloud 占位文件）。"""
    return sum(1 for p in vault.rglob("*.md.icloud"))


def trash_folders(orphans: list[dict]) -> tuple[int, list[str]]:
    moved, errors = 0, []
    for o in orphans:
        result = subprocess.run(["trash", str(o["path"])], capture_output=True, text=True)
        if result.returncode == 0:
            moved += 1
        else:
            errors.append(f"{o['path'].name}：{result.stderr.strip() or result.returncode}")
    return moved, errors


def auto_clean(vault: Path, root: Path, state: SyncState) -> None:
    """sync.py 结束时调用：自动把已删除笔记的媒体移到废纸篓。有任何可疑情况就不动，只提示。"""
    orphans = find_orphan_media(root / "Media", vault_note_ids(vault), state)
    if not orphans:
        return
    size = sum(o["bytes"] for o in orphans) / 1e6
    pending = icloud_pending(vault)
    total = sum(1 for d in (root / "Media").iterdir() if d.is_dir())
    if pending:
        print(f"\n⚠️ iCloud 里还有 {pending} 篇笔记没下载到这台 Mac，为了安全，这次不自动清理媒体。")
        return
    if len(orphans) > 10 and len(orphans) > total / 2:
        print(f"\n⚠️ 有 {len(orphans)} 篇笔记的图片/视频找不到对应笔记（占全部的一半以上），数量异常，这次不自动删除。")
        print("   如果确实是你删的，运行 .venv/bin/python cleanup.py 确认后清理。")
        return
    if not shutil.which("trash"):
        print("\n⚠️ 这台 Mac 没有 trash 命令（需要 macOS 14 或更新），没法自动清理媒体。")
        return
    moved, errors = trash_folders(orphans)
    if moved:
        print(f"\n🧹 你在 Obsidian 里删掉了 {len(orphans)} 篇笔记，已把其中 {moved} 篇的图片/视频移到废纸篓（共 {size:.0f} MB）。")
    for e in errors:
        print(f"  ✗ 没能移走 {e}")


def main() -> int:
    cfg = load_config()
    root = cfg["vault_path"] / cfg["root_dir"]
    state = SyncState.load(root)
    orphans = find_orphan_media(root / "Media", vault_note_ids(cfg["vault_path"]), state)
    if not orphans:
        print("没有需要清理的图片/视频。")
        return 0
    print(f"这 {len(orphans)} 篇笔记已在 Obsidian 里删除，但图片/视频还在：")
    for o in orphans:
        print(f"  {o['title'][:30]}  —— {o['bytes'] / 1e6:.1f} MB")
    print(f"共 {sum(o['bytes'] for o in orphans) / 1e6:.0f} MB。")
    if not shutil.which("trash"):
        print("⛔ 这台 Mac 上没有 trash 命令（需要 macOS 14 或更新），请手动删除上面这些文件夹。")
        return 1
    answer = input("把它们移到废纸篓吗？之后可以在废纸篓里「放回原处」。输入 y 确认：")
    if answer.strip().lower() != "y":
        print("已取消，什么都没动。")
        return 0
    moved, errors = trash_folders(orphans)
    for e in errors:
        print(f"  ✗ 没能移走 {e}")
    print(f"已移到废纸篓：{moved} 个文件夹。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
