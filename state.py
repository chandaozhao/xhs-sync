"""M5 同步记录：state.json 记下哪些笔记已同步、哪些失败待重试。

补齐策略：
- 旧收藏还没补齐时：从新到旧找，跳过已同步的，继续往下找没同步的，每次最多 N 篇
- 扫到收藏列表末尾一次后，旧收藏就算补齐了；之后遇到已同步的笔记就停止（只同步新收藏）
- 有失败待重试的笔记时，也会跳过已同步的继续往下找，把它们找出来重试
- 同一篇失败 3 次就放弃（比如笔记已被删除），不再拖慢以后的同步

你在 Obsidian 里删掉笔记：已同步的不会再下载（记录还在）；待重试的也不再重试。
state.json 会在 Vault 里自动备份一份（RedNote/.xhs-sync-state.json，Obsidian 里看不到），
项目文件夹里的丢了会从备份恢复，这样删过的笔记也不会被重新下载。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from xhs_common import PROJECT_DIR

STATE_PATH = PROJECT_DIR / "state.json"
BACKUP_NAME = ".xhs-sync-state.json"  # 放在 Vault 的 RedNote 文件夹里
MAX_ATTEMPTS = 3
_NOTE_ID_RE = re.compile(r'^id: "?([0-9a-f]{24})"?\s*$', re.M)


def vault_note_ids(vault: Path) -> set[str]:
    """整个 Vault 里现存的小红书笔记 id（按 frontmatter 的 id 找，笔记被移到别的文件夹也算）。

    跳过 .trash 等隐藏文件夹：在 Obsidian 里删除、进了 .trash 的笔记算已删除。
    """
    ids = set()
    for p in vault.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(vault).parts):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                head = f.read(600)
        except (OSError, UnicodeDecodeError):
            continue
        m = _NOTE_ID_RE.search(head)
        if m:
            ids.add(m.group(1))
    return ids


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read_frontmatter_value(line: str) -> str:
    value = line.split(":", 1)[1].strip()
    try:
        return json.loads(value)
    except ValueError:
        return value


class SyncState:
    def __init__(self, data: dict, path: Path, backup: Path | None = None):
        self.path = path
        self.backup = backup
        self.synced: dict = data.get("synced", {})
        self.failed: dict = data.get("failed", {})
        self.gave_up: dict = data.get("gaveUp", {})
        self.backfill_done: bool = data.get("backfillDone", False)

    @classmethod
    def load(cls, root_dir: Path, path: Path = STATE_PATH) -> "SyncState":
        """root_dir 是 Vault 里的 RedNote 文件夹。"""
        backup = root_dir / BACKUP_NAME
        found = []
        for src in (path, backup):
            try:
                found.append((json.loads(src.read_text("utf-8")), src))
            except (OSError, ValueError):
                pass  # 不存在，或者 iCloud 还没下载完
        if found:
            # 两份都有时用较新的（比如换了一台 Mac 运行，Vault 里的备份会更新）
            data, src = max(found, key=lambda f: f[0].get("updatedAt", ""))
            if src == backup and len(found) == 1:
                print("项目文件夹里没有 state.json，已从 Vault 里的备份恢复同步记录。")
            elif src == backup:
                print("Vault 里的同步记录比项目里的新（可能在另一台 Mac 上运行过），使用 Vault 里的。")
            return cls(data, path, backup)
        state = cls({}, path, backup)
        state._seed_from_vault(root_dir / "Bookmarks")
        return state

    def _seed_from_vault(self, bookmarks_dir: Path) -> None:
        """第一次运行（或 state.json 丢了）时，把 Obsidian 里已有的笔记记为已同步，避免重复下载。"""
        for p in sorted(bookmarks_dir.glob("*.md")) if bookmarks_dir.is_dir() else []:
            try:
                head = p.read_text("utf-8")[:600].splitlines()
            except OSError:
                continue
            fields = {l.split(":", 1)[0]: _read_frontmatter_value(l) for l in head if l.startswith(("id:", "title:"))}
            if fields.get("id"):
                self.synced[fields["id"]] = {"title": fields.get("title", ""), "file": p.name, "syncedAt": "（从 Obsidian 恢复）"}
        if self.synced:
            print(f"从 Obsidian 里已有的笔记恢复了 {len(self.synced)} 条同步记录。")

    def save(self) -> None:
        data = {
            "synced": self.synced, "failed": self.failed, "gaveUp": self.gave_up,
            "backfillDone": self.backfill_done, "updatedAt": _now(),
        }
        text = json.dumps(data, ensure_ascii=False, indent=1)
        for target in (self.path, self.backup):
            if target is None or not target.parent.is_dir():
                continue
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, "utf-8")
            tmp.replace(target)

    # ------------------------------------------------------------ 给收藏列表用

    def known_ids(self) -> set[str]:
        """不需要再处理的笔记：已同步的 + 已放弃的。失败待重试的不算。"""
        return set(self.synced) | set(self.gave_up)

    def stop_at_known(self) -> bool:
        """旧收藏已补齐、且没有待重试的笔记时，遇到已同步的就停止。"""
        return self.backfill_done and not self.failed

    def mode_text(self) -> str:
        if not self.backfill_done:
            return "补齐旧收藏（跳过已同步的，继续往下找）"
        if self.failed:
            return "只同步新收藏 + 重试失败的笔记"
        return "只同步新收藏（遇到已同步的就停止）"

    def after_scan(self, scan: dict) -> None:
        """收藏列表扫到末尾时：标记旧收藏已补齐，清理已经不在收藏里的失败记录。"""
        if not scan["end_reached"]:
            return
        if not self.backfill_done:
            self.backfill_done = True
            print("📌 已经看过整个收藏列表：这批完成后，旧收藏就补齐了，以后只同步新收藏。")
        seen = set(scan["seen_ids"])
        for nid in [i for i in self.failed if i not in seen]:
            info = self.failed.pop(nid)
            print(f"  （「{info['title']}」已不在收藏里，不再重试）")

    # ------------------------------------------------------------ 记录结果

    def mark_synced(self, item: dict, file_name: str) -> None:
        self.failed.pop(item["id"], None)
        self.synced[item["id"]] = {"title": item["title"], "file": file_name, "syncedAt": _now()}

    def skip_deleted(self, present_ids: set[str]) -> list[str]:
        """待重试的笔记如果已经写进过 Obsidian、又被你删掉了，就不再重试。返回这些笔记的标题。"""
        titles = []
        for nid in [i for i, info in self.failed.items() if info.get("file") and i not in present_ids]:
            info = self.failed.pop(nid)
            self.synced[nid] = {"title": info["title"], "file": info["file"], "syncedAt": info["lastTried"],
                                "deletedInObsidian": True}
            titles.append(info["title"])
        return titles

    def mark_failed(self, item: dict, error: str, file_name: str | None = None) -> bool:
        """记录失败。file_name：笔记文字已写进 Obsidian 时的文件名。返回 True 表示失败次数已满、放弃这篇。"""
        prev = self.failed.pop(item["id"], {})
        info = {"title": item["title"], "error": error, "attempts": prev.get("attempts", 0) + 1, "lastTried": _now()}
        if file_name or prev.get("file"):
            info["file"] = file_name or prev["file"]
        if info["attempts"] >= MAX_ATTEMPTS:
            self.gave_up[item["id"]] = info
            return True
        self.failed[item["id"]] = info
        return False
