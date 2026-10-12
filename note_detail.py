"""M3 笔记详情：从详情页的 __INITIAL_STATE__ 读取笔记，整理字段，生成 Markdown 写入 Obsidian。

字段来源（M3 探索的真实结构）：__INITIAL_STATE__.note.noteDetailMap[笔记id].note
  title / desc / type / time / lastUpdateTime / ipLocation
  user.nickname / user.userId
  tagList[].name
  interactInfo.likedCount / collectedCount / commentCount（字符串，如 "634"、"1.2万"）
  imageList[].urlDefault / urlPre / infoList[{imageScene, url}]
  video.media.stream.<编码>[].masterUrl / backupUrls / format / audioCodec / width / height
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, Page

# 读取一篇笔记的详情。顺手解开 Vue 的 ref，并防止循环引用。
_READ_NOTE_JS = """
(id) => {
  const unref = v => (v && v.__v_isRef) ? v._value : v;
  const stack = new WeakSet();
  const walk = (v, d) => {
    v = unref(v);
    if (v === null || typeof v !== 'object') return typeof v === 'function' ? undefined : v;
    if (d > 20 || stack.has(v)) return null;
    stack.add(v);
    let out;
    if (Array.isArray(v)) out = v.map(x => walk(x, d + 1));
    else { out = {}; for (const k of Object.keys(v)) { if (!k.startsWith('__v_')) out[k] = walk(v[k], d + 1); } }
    stack.delete(v);
    return out;
  };
  const s = window.__INITIAL_STATE__;
  const map = s && unref(s.note) && unref(unref(s.note).noteDetailMap);
  const entry = map && unref(map[id]);
  return entry && entry.note ? walk(entry.note, 0) : null;
}
"""

# 笔记打不开时页面上常见的提示
UNAVAILABLE_WORDS = ("无法浏览", "不存在", "已删除", "已被删除", "违规", "仅作者可见", "审核中")


class NoteError(Exception):
    """单篇笔记处理失败（比如笔记已删除）。只跳过这一篇，不影响其他笔记。

    file：如果笔记文字已经写进了 Obsidian（只是媒体不完整），这里是文件名。
    """

    def __init__(self, message: str, file: str | None = None):
        super().__init__(message)
        self.file = file


# ---------------------------------------------------------------- 读取

def read_note_raw(page: Page, note_id: str) -> dict:
    try:
        raw = page.evaluate(_READ_NOTE_JS, note_id)
    except PlaywrightError as e:
        raise NoteError(f"读取页面数据失败：{str(e).splitlines()[0]}") from e
    if raw and raw.get("noteId"):
        return raw
    try:
        text = page.inner_text("body")[:3000]
    except PlaywrightError:
        text = ""
    hint = next((w for w in UNAVAILABLE_WORDS if w in text), None)
    if hint:
        raise NoteError(f"笔记无法查看（页面提示含「{hint}」，可能已删除或设为私密）")
    raise NoteError("页面里没有找到这篇笔记的数据")


# ---------------------------------------------------------------- 整理字段

def parse_count(value) -> int | str:
    """把 "634"、"1万"、"1.2万"、"10万+" 转成整数；转不了就保留原文。"""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "").strip().rstrip("+")
    if not s:
        return 0
    mult = 1
    for unit, m in (("亿", 10**8), ("万", 10**4), ("w", 10**4), ("W", 10**4), ("千", 10**3), ("k", 10**3), ("K", 10**3)):
        if s.endswith(unit):
            s, mult = s[: -len(unit)], m
            break
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return str(value)


def to_tag(name: str) -> str:
    """变成 Obsidian 能识别的标签：只保留文字、数字、_、-、/，其他字符换成 _。"""
    t = re.sub(r"[^\w\-/]+", "_", name.strip()).strip("_")
    if t.isdigit():
        t = "_" + t  # Obsidian 的标签不能是纯数字
    return t


_TOPIC_RE = re.compile(r"#([^#\[\]\n]+?)\[[^\]\n]*\]#")  # 正文里的话题，如 #PersonalAI[话题]#


def clean_desc(desc: str) -> str:
    text = _TOPIC_RE.sub(lambda m: "#" + to_tag(m.group(1)), desc or "")
    text = text.replace("\t", "")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pick_image_url(img: dict) -> str | None:
    if img.get("urlDefault"):
        return img["urlDefault"]
    for info in img.get("infoList") or []:
        if info.get("imageScene") == "WB_DFT" and info.get("url"):
            return info["url"]
    return img.get("urlPre") or img.get("url") or None


def pick_video(video: dict | None, quality: str = "lowest") -> dict | None:
    """在所有视频流里挑一个。

    先只看「有声音的 mp4」（没有就退而求其次），再按 quality 挑分辨率最低或最高的。
    有的流只有画面没有声音（比如 fmp4 格式的），这种不选。
    """
    streams = ((video or {}).get("media") or {}).get("stream") or {}
    cands = []
    for codec, lst in streams.items():
        for s in lst or []:
            url = s.get("masterUrl") or next(iter(s.get("backupUrls") or []), None)
            if not url:
                continue
            has_audio = bool(s.get("audioCodec")) or (s.get("audioChannels") or 0) > 0
            is_mp4 = s.get("format") == "mp4" or ".mp4" in url.split("?")[0]
            cands.append({
                "url": url, "backup_urls": s.get("backupUrls") or [], "codec": codec,
                "format": s.get("format"), "has_audio": has_audio, "is_mp4": is_mp4,
                "width": s.get("width") or 0, "height": s.get("height") or 0,
                "bitrate": s.get("avgBitrate") or 0, "size": s.get("size") or 0,
                "duration": round((s.get("duration") or 0) / 1000),
            })
    if not cands:
        return None
    usable = ([c for c in cands if c["has_audio"] and c["is_mp4"]]
              or [c for c in cands if c["has_audio"]] or cands)
    rank = lambda c: (c["width"] * c["height"] or 10**9, c["bitrate"])  # 分辨率未知的当作很大
    best = min(usable, key=rank) if quality == "lowest" else max(usable, key=rank)
    return {**best, "options": len(usable)}


def fmt_time(ms) -> str:
    """毫秒时间戳 → 本地时间，Obsidian 能识别的格式：2026-10-12T20:20:22"""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S") if ms else ""


def parse_note(raw: dict, href: str, video_quality: str = "lowest") -> dict:
    title = (raw.get("title") or "").strip()
    desc = clean_desc(raw.get("desc") or "")
    if not title:  # 有些笔记没有标题：用正文第一行代替
        first = next((l for l in desc.splitlines() if l.strip() and not l.strip().startswith("#")), "")
        title = first.strip()[:30] or "无标题"
    user = raw.get("user") or {}
    info = raw.get("interactInfo") or {}
    images = [{"url": u, "width": i.get("width"), "height": i.get("height")}
              for i in raw.get("imageList") or [] if (u := pick_image_url(i))]
    return {
        "id": raw["noteId"],
        "title": title,
        "type": raw.get("type") or "",
        "author": user.get("nickname") or user.get("nickName") or "",
        "author_id": user.get("userId") or "",
        "url": href,
        "tags": [t for t in (to_tag(x.get("name") or "") for x in raw.get("tagList") or []) if t],
        "desc": desc,
        "createdAt": fmt_time(raw.get("time")),
        "updatedAt": fmt_time(raw.get("lastUpdateTime")),
        "ipLocation": raw.get("ipLocation") or "",
        "likes": parse_count(info.get("likedCount")),
        "collects": parse_count(info.get("collectedCount")),
        "comments": parse_count(info.get("commentCount")),
        "images": images,
        "video": pick_video(raw.get("video"), video_quality),
    }


# ---------------------------------------------------------------- 媒体文件名

def guess_image_ext(url: str) -> str:
    """从文件名部分猜扩展名（如 …/1040g0k0…!nd_dft_wlteh_webp_3）。域名 sns-webpic 不算。"""
    u = urlparse(url).path.rsplit("/", 1)[-1].lower()
    for ext in ("webp", "png", "gif", "jpeg", "jpg", "heic"):
        if ext in u:
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


def plan_media(note: dict) -> list[dict]:
    """计划要保存的媒体文件：[{kind, url, backup_urls, filename}]。M4 会按这个清单下载。"""
    items = []
    if note["video"]:
        v = note["video"]
        items.append({"kind": "video", "url": v["url"], "backup_urls": v["backup_urls"], "filename": "video.mp4"})
    for i, img in enumerate(note["images"], 1):
        items.append({"kind": "image", "url": img["url"], "backup_urls": [],
                      "filename": f"{i:02d}.{guess_image_ext(img['url'])}"})
    return items


# ---------------------------------------------------------------- Markdown

def _q(value) -> str:
    """YAML 字符串：用 JSON 的双引号写法，最省心也最安全。"""
    return json.dumps(value, ensure_ascii=False)


def render_markdown(note: dict, media: list[dict], synced_at: str, media_rel_dir: str) -> str:
    lines = [
        "---",
        f"id: {_q(note['id'])}",
        f"title: {_q(note['title'])}",
        f"type: {note['type']}",
        f"author: {_q(note['author'])}",
        f"url: {_q(note['url'])}",
        "tags:" if note["tags"] else "tags: []",
        *[f"  - {_q(t)}" for t in note["tags"]],
        f"createdAt: {note['createdAt']}",
        f"syncedAt: {synced_at}",
        f"likes: {_q(note['likes']) if isinstance(note['likes'], str) else note['likes']}",
        f"collects: {_q(note['collects']) if isinstance(note['collects'], str) else note['collects']}",
        f"comments: {_q(note['comments']) if isinstance(note['comments'], str) else note['comments']}",
        "---",
        "",
        f"# {note['title']}",
        "",
    ]
    if note["desc"]:
        lines += [note["desc"], ""]
    for m in media:
        lines += [f"![]({media_rel_dir}/{m['filename']})", ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 文字稿区块（transcribe.py 写入）
#
# 视频下方一个默认折叠的 callout：
#   > [!note]- 文字稿（自动转录）
#   > 第一段……
# 只认这个区块，替换时不碰笔记里的其他内容。用户改了 callout 类型或折叠符号也认得出。

TRANSCRIPT_HEADER = "> [!note]- 文字稿（自动转录）"
_TRANSCRIPT_HEADER_RE = re.compile(r"^>\s*\[!\w+\][+-]?\s*文字稿")
_VIDEO_EMBED_RE = re.compile(r"^!\[[^\]]*\]\([^)]*/video\.\w+\)\s*$|^!\[\[[^\]]*video\.\w+[^\]]*\]\]\s*$")


def find_transcript_block(markdown: str) -> str | None:
    """笔记里已有的文字稿区块（原样返回），没有就返回 None。"""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if _TRANSCRIPT_HEADER_RE.match(line):
            j = i + 1
            while j < len(lines) and lines[j].startswith(">"):
                j += 1
            return "\n".join(lines[i:j])
    return None


def put_transcript_block(markdown: str, block: str) -> str:
    """放入文字稿区块：已有就原地替换；没有就插在视频下方（找不到视频就放在最后）。"""
    lines = markdown.rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        if _TRANSCRIPT_HEADER_RE.match(line):
            j = i + 1
            while j < len(lines) and lines[j].startswith(">"):
                j += 1
            lines[i:j] = block.split("\n")
            return "\n".join(lines) + "\n"
    at = next((i + 1 for i, line in enumerate(lines) if _VIDEO_EMBED_RE.match(line)), len(lines))
    rest = lines[at:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    new = lines[:at] + ["", *block.split("\n")] + (["", *rest] if rest else [])
    return "\n".join(new) + "\n"


def get_frontmatter(markdown: str, key: str) -> str | None:
    """frontmatter 里某个字段的原始值（不解析），没有就返回 None。"""
    lines = markdown.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None


def set_frontmatter(markdown: str, key: str, value: str) -> str:
    """设置 frontmatter 里的一个字段：已有就改，没有就加在最后。"""
    lines = markdown.split("\n")
    if not lines or lines[0].strip() != "---":
        return markdown
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            lines.insert(i, f"{key}: {value}")
            return "\n".join(lines)
        if line.startswith(f"{key}:"):
            lines[i] = f"{key}: {value}"
            return "\n".join(lines)
    return markdown


def keep_transcript(old: str, new: str) -> str:
    """重新写同一篇笔记时，把旧笔记里的文字稿区块和 transcribed 标记带过来。

    用户删掉了文字稿区块但标记还在：只保留标记，这样不会被重新转录加回来。
    """
    if get_frontmatter(old, "transcribed") != "true":
        return new
    block = find_transcript_block(old)
    if block:
        new = put_transcript_block(new, block)
    return set_frontmatter(new, "transcribed", "true")


# ---------------------------------------------------------------- 写文件

def safe_filename(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#^\[\]\r\n\t]', " ", title)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s[:60].strip() or "无标题"


def find_note_file(folder: Path, note_id: str) -> Path | None:
    """按 frontmatter 里的 id 找已有文件（标题改了也能找到，避免重复）。"""
    needle = f"id: {_q(note_id)}"
    for p in folder.glob("*.md"):
        try:
            with open(p, encoding="utf-8") as f:
                head = f.read(300)
        except OSError:
            continue
        if needle in head:
            return p
    return None


def write_note(folder: Path, note: dict, markdown: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = find_note_file(folder, note["id"])
    if path is None:
        base = safe_filename(note["title"])
        path = folder / f"{base}.md"
        if path.exists():  # 同名但不是同一篇
            path = folder / f"{base} ({note['id'][-6:]}).md"
    else:  # 重写已有笔记（比如失败重试）：保留已经转录好的文字稿
        markdown = keep_transcript(path.read_text("utf-8"), markdown)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(markdown, "utf-8")
    tmp.replace(path)  # 先写临时文件再替换，避免 iCloud 同步到写了一半的文件
    return path
