"""视频转文字：把视频笔记里说的话转成文字稿，放进笔记（视频下方一个默认折叠的「文字稿」）。

- 完全在本机转录：whisper.cpp + large-v3-turbo 模型，用 Mac 的 GPU 加速，不上传任何东西
- 处理 Vault 里 type: video、还没转录过的笔记（frontmatter 里没有 transcribed: true）
- 视频按笔记 id 找：<Vault>/RedNote/Media/<id>/video.mp4。视频保留，不删
- 只插入/替换文字稿区块，你对笔记做的其他修改不受影响；删掉文字稿区块也不会被加回来

运行：.venv/bin/python transcribe.py                 转录所有还没转录的视频
     .venv/bin/python transcribe.py --limit 3       这次只转 3 篇
     .venv/bin/python transcribe.py --redo <笔记id> [<笔记id> …]  重新转录这几篇（覆盖旧的文字稿）
sync.py 结束时也会自动转录新同步的视频（config.toml 的 auto_transcribe）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from note_detail import TRANSCRIPT_HEADER, get_frontmatter, put_transcript_block, set_frontmatter
from state import _NOTE_ID_RE
from xhs_common import load_config

# 提示词：让模型输出简体中文、带标点
PROMPTS = {"zh": "以下是普通话的句子，使用简体中文。"}
# 一段文字稿攒到这么多字就换段
PARAGRAPH_CHARS = 150
# Whisper 在纯音乐/没人说话的地方常会「幻听」出训练数据里的字幕署名，这些整段丢掉。
# 实际遇到过：「(中文)字幕志愿者 杨茜茜/李宗盛」「词曲 李宗盛」「字幕:CM 李宗盛」「字幕:CMG」
# 「优优独播剧场——YoYo Television Series Exclusive」
HALLUCINATION_RE = re.compile(
    r"字幕.{0,8}(志愿者|提供|制作|校对|翻译|by)|字幕\s*[:：]\s*[A-Za-z]{2,6}\b|(词曲|作词|作曲).{0,4}李宗盛|"
    r"Amara\.org|明镜.{0,4}点点|点赞.{0,4}订阅.{0,4}转发|"
    r"优优独播剧场|YoYo Television|谢谢收看.{0,2}下集再见", re.I)
# 中文文字稿里的半角标点换成全角；两边都是英文/数字的不换（如 10:30、Hello, world）
_HALF_TO_FULL = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}
_HALF_PUNCT_RE = re.compile(r"(?<![A-Za-z0-9])[,?!:;]|[,?!:;](?![A-Za-z0-9 ])")
_CJK = r"　-〿一-鿿＀-￯"


class TranscribeUnavailable(Exception):
    """转录工具没装好。"""


class TranscribeError(Exception):
    """单个视频转录失败。只跳过这一篇。"""


# ---------------------------------------------------------------- 工具

def _which(name: str) -> str | None:
    # 从「快捷指令」或定时任务运行时 PATH 里可能没有 Homebrew 的目录
    return shutil.which(name) or next(
        (str(p) for p in (Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name) if p.exists()), None)


def find_tools(cfg: dict) -> dict:
    whisper, ffmpeg, model = _which("whisper-cli"), _which("ffmpeg"), cfg["whisper_model"]
    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not whisper:
        missing.append("whisper.cpp")
    if missing:
        raise TranscribeUnavailable(f"还没安装 {'、'.join(missing)}。在终端运行：brew install {' '.join(missing)}")
    if not model.is_file():
        raise TranscribeUnavailable(f"找不到语音模型文件 {model}。运行 bash setup.sh 会自动下载。")
    return {"whisper": whisper, "ffmpeg": ffmpeg, "model": str(model)}


# ---------------------------------------------------------------- 找要转录的笔记

def video_notes(vault: Path) -> list[dict]:
    """整个 Vault 里的小红书视频笔记（笔记被挪到别的文件夹也能找到；跳过 .trash 等隐藏文件夹）。"""
    notes = []
    for p in sorted(vault.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(vault).parts):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                head = f.read(600)
            if not _NOTE_ID_RE.search(head):
                continue
            text = p.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if (get_frontmatter(text, "type") or "").strip('"') != "video":
            continue
        notes.append({
            "id": _NOTE_ID_RE.search(head).group(1),
            "path": p,
            "title": (get_frontmatter(text, "title") or p.stem).strip('"'),
            "transcribed": get_frontmatter(text, "transcribed") == "true",
        })
    return notes


def find_video(media_dir: Path) -> Path | None:
    """笔记的视频文件。还在 iCloud 云端没下载下来时抛出 TranscribeError。"""
    videos = [p for p in media_dir.glob("video.*") if p.suffix not in (".part", ".icloud")]
    if videos:
        return videos[0]
    if list(media_dir.glob(".video.*.icloud")):
        raise TranscribeError("视频还在 iCloud 云端，没下载到这台 Mac（在 Finder 里点一下云朵图标下载后再试）")
    return None


# ---------------------------------------------------------------- 转录

def extract_audio(ffmpeg: str, video: Path, wav: Path) -> bool:
    """提取成 whisper 需要的 16 kHz 单声道 wav。视频没有声音时返回 False。"""
    r = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(video),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True
    if "does not contain any stream" in r.stderr:
        return False
    raise TranscribeError(f"提取音频失败：{(r.stderr.strip().splitlines() or ['未知错误'])[-1]}")


def run_whisper(tools: dict, wav: Path, language: str) -> tuple[list[str], str]:
    """返回 (每一小段的文字, 识别出的语言)。"""
    out = wav.with_suffix("")
    cmd = [tools["whisper"], "-m", tools["model"], "-l", language, "-oj", "-of", str(out), "-np", "-f", str(wav)]
    if language in PROMPTS:
        cmd += ["--prompt", PROMPTS[language]]
    seconds = wav.stat().st_size / 32000  # 16 kHz × 16 位 = 每秒 32000 字节
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(600, seconds * 2))
    except subprocess.TimeoutExpired:
        raise TranscribeError("转录超时") from None
    if r.returncode != 0:
        raise TranscribeError(f"转录失败：{(r.stderr.strip().splitlines() or ['未知错误'])[-1]}")
    data = json.loads(out.with_suffix(".json").read_text("utf-8"))
    texts = [(seg.get("text") or "").strip() for seg in data.get("transcription") or []]
    return texts, (data.get("result") or {}).get("language") or language


def _is_cjk(ch: str) -> bool:
    return bool(re.match(f"[{_CJK}]", ch))


def format_transcript(texts: list[str], language: str) -> list[str]:
    """整理成段落：去掉幻听的字幕署名和连续重复，中文用全角标点，几小段合成一段。"""
    kept, last = [], None
    for t in texts:
        t = re.sub(r"\s+", " ", t).strip()
        if not t or HALLUCINATION_RE.search(t) or t == last:
            continue
        last = t
        if language == "zh":
            t = _HALF_PUNCT_RE.sub(lambda m: _HALF_TO_FULL[m.group()], t)
        kept.append(t)
    paragraphs, cur = [], ""
    for t in kept:
        if cur and not _is_cjk(cur[-1]) and not _is_cjk(t[0]):
            cur += " "  # 英文单词之间要有空格；中文直接接上
        cur += t
        if len(cur) >= PARAGRAPH_CHARS:
            paragraphs.append(cur)
            cur = ""
    if cur:
        paragraphs.append(cur)
    return paragraphs


def make_block(paragraphs: list[str], has_audio: bool = True) -> str:
    lines = [TRANSCRIPT_HEADER]
    if not has_audio:
        lines.append("> （这个视频没有声音。）")
    elif not paragraphs:
        lines.append("> （没有识别到说话内容，可能只有背景音乐。）")
    for i, p in enumerate(paragraphs):
        if i:
            lines.append(">")
        lines.append(f"> {p}")
    return "\n".join(lines)


def transcribe_note(tools: dict, cfg: dict, note: dict) -> int:
    """转录一篇笔记的视频并写进笔记。返回文字稿字数。"""
    media_dir = cfg["vault_path"] / cfg["root_dir"] / "Media" / note["id"]
    video = find_video(media_dir)
    if video is None:
        raise TranscribeError("找不到视频文件（可能还没下载成功，下次同步后会再试）")
    with tempfile.TemporaryDirectory(prefix="xhs-transcribe-") as tmp:
        wav = Path(tmp) / "audio.wav"
        has_audio = extract_audio(tools["ffmpeg"], video, wav)
        texts, lang = run_whisper(tools, wav, cfg["transcribe_language"]) if has_audio else ([], "")
    paragraphs = format_transcript(texts, lang)
    # 转录要花点时间：写入前重新读一遍笔记，不覆盖这期间你在 Obsidian 里做的修改
    path = note["path"]
    try:
        text = path.read_text("utf-8")
    except OSError:
        raise TranscribeError("笔记在转录期间被删除或移走了，跳过") from None
    text = put_transcript_block(text, make_block(paragraphs, has_audio))
    text = set_frontmatter(text, "transcribed", "true")
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, "utf-8")
    tmp_path.replace(path)  # 先写临时文件再替换，避免 iCloud 同步到写了一半的文件
    return sum(len(p) for p in paragraphs)


def transcribe_all(tools: dict, cfg: dict, notes: list[dict]) -> tuple[int, int]:
    """逐篇转录。返回 (成功数, 失败数)。按 Ctrl+C 可以随时停下，已转好的都已保存。"""
    done = failed = 0
    for n, note in enumerate(notes, 1):
        print(f"[{n}/{len(notes)}] {note['title']}")
        start = time.time()
        try:
            chars = transcribe_note(tools, cfg, note)
        except TranscribeError as e:
            failed += 1
            print(f"  ✗ {e}")
            continue
        done += 1
        print(f"  ✓ {chars} 字，用时 {time.time() - start:.0f} 秒")
    return done, failed


# ---------------------------------------------------------------- 入口

def pending_notes(cfg: dict, redo: list[str] | None = None) -> list[dict]:
    notes = video_notes(cfg["vault_path"])
    if redo:
        return [n for n in notes if n["id"] in redo]
    return [n for n in notes if not n["transcribed"]]


def transcribe_after_sync(cfg: dict) -> None:
    """sync.py 结束时调用：转录还没转录的视频。工具没装好时只提示一句。"""
    notes = pending_notes(cfg)
    if not notes:
        return
    try:
        tools = find_tools(cfg)
    except TranscribeUnavailable as e:
        print(f"\n提示：有 {len(notes)} 个视频还没转成文字稿，但{e}")
        return
    print(f"\n🎙️ 把 {len(notes)} 个视频转成文字稿（在本机转录，按 Ctrl+C 可以跳过）…")
    try:
        done, failed = transcribe_all(tools, cfg, notes)
    except KeyboardInterrupt:
        print("\n已跳过剩下的转录，下次同步时会继续。")
        return
    print(f"转录：成功 {done} 个" + (f"，失败 {failed} 个" if failed else "") + "。")


def main() -> int:
    parser = argparse.ArgumentParser(description="把视频笔记的语音转成文字稿")
    parser.add_argument("--limit", type=int, help="这次最多转录多少篇")
    parser.add_argument("--redo", nargs="+", metavar="笔记id", help="重新转录这几篇（覆盖旧的文字稿）")
    args = parser.parse_args()

    cfg = load_config()
    try:
        tools = find_tools(cfg)
    except TranscribeUnavailable as e:
        print(f"⛔ {e}")
        return 1
    notes = pending_notes(cfg, args.redo)
    missing = [i for i in args.redo or [] if i not in {n["id"] for n in notes}]
    if missing:
        print(f"⛔ Vault 里没有找到这些 id 的视频笔记：{'、'.join(missing)}")
        return 1
    if not notes:
        print("所有视频都已经转成文字稿了。")
        return 0
    if args.limit:
        notes = notes[: args.limit]
    print(f"要转录 {len(notes)} 个视频。在本机转录，按 Ctrl+C 可以随时停下（已转好的都已保存）。\n")
    try:
        done, failed = transcribe_all(tools, cfg, notes)
    except KeyboardInterrupt:
        print("\n已手动中断（已转好的都已保存）。")
        return 130
    print(f"\n完成：成功 {done} 个" + (f"，失败 {failed} 个" if failed else "") + "。")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
