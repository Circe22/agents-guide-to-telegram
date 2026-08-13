#!/usr/bin/env python3
"""tg_sticker —— 贴纸车道：库、认领、挑选、懒迁移、句内标记。

设计要点（背景与判断标准见 COOKBOOK「贴纸」一章）：

- **认人认 `file_unique_id`（跨 bot 恒定），发送用 `file_id`（绑 bot、会变）**，
  分工不可互换。
- **file_id 是「这个 bot」的资产**：按 bot 分开缓存（file-ids.<bot_id>.json）；
  只有 Telegram 明确回 400 才判 ID 失效、才从归档原图重传。
  网络错/限流/5xx 都不算——判错了会重复上传。
- **emoji 是索引键不是描述**：一个 emoji＝池里随机（避开上次刚发的那张）；
  多个 emoji＝交集收窄；交集为空＝不发（发错脸是说错话，不发是没说话）。
- 本模块**不碰网络**：Bot API 调用（api / download）由 server 注入，
  测试直接换成假的。

⚠️ 这条管道属于 MCP server，**绝不能往 stdout 打字**。调试写 stderr。
"""

from __future__ import annotations

import json
import os
import random
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

STICKER_EXTS = (".webp", ".tgs", ".webm")
STICKER_MAX_BYTES = 10 * 1024 * 1024   # 贴纸不该有 10MB，超了多半拿错了文件
DEFAULT_MARKER_MAX = 3                  # 一条消息最多剥几张，多出来的原样留在正文

_VARIATION_SELECTORS = {"\ufe0f", "\ufe0e"}   # VS16 / VS15
_ZWJ = "\u200d"   # zero-width joiner
_SKIN_LO, _SKIN_HI = "\U0001F3FB", "\U0001F3FF"
_RI_LO, _RI_HI = "\U0001F1E6", "\U0001F1FF"   # regional indicator（旗帜的两半）


class ApiRejected(RuntimeError):
    """Telegram API 明确拒收（payload.ok == false）。

    `code` 是 error_code。挑「file_id 失效」只认 400——别拿报错文案当接口。
    """

    def __init__(self, description: str, code: int = 0) -> None:
        super().__init__(description)
        self.code = code


# ---------- emoji 处理 ----------
def _strip_vs(text: str) -> str:
    return "".join(ch for ch in text if ch not in _VARIATION_SELECTORS)


def split_clusters(text: str) -> list[str]:
    """把一串 emoji 按「字素簇」切开（简化版，够 emoji 用）。

    规则：ZWJ 连接的整串不拆（🧑‍💻 是一个）；肤色修饰跟着基字；
    两个 regional indicator 是一面旗。⚠️ 调用方应**先整串查表**，
    查不到才来这儿拆——这是 ZWJ 组合字不散架的第一道保险。
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        j = i + 1
        # 旗帜：两个 regional indicator 配一对
        if _RI_LO <= text[i] <= _RI_HI and j < len(text) and _RI_LO <= text[j] <= _RI_HI:
            j += 1
        while j < len(text):
            ch = text[j]
            if ch in _VARIATION_SELECTORS or _SKIN_LO <= ch <= _SKIN_HI:
                j += 1
                continue
            if ch == _ZWJ and j + 1 < len(text):
                j += 2   # ZWJ 连同它接的下一个基字
                continue
            break
        out.append(text[i:j])
        i = j
    return out


def _looks_textual(content: str) -> bool:
    """括号里出现字母/数字/汉字/空白 ⇒ 是普通括号话，不是贴纸标记。"""
    for ch in content:
        if ch.isspace():
            return True
        cat = unicodedata.category(ch)
        if cat.startswith("L") or cat.startswith("N"):
            return True
    return False


# ---------- 库 ----------
def sticker_dir() -> Path:
    """库目录：TG_STICKER_DIR > 配置文件 sticker_dir > ~/.tg-rich-mcp-stickers。"""
    env = (os.environ.get("TG_STICKER_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from tg_rich_mcp import _file_config
        conf = str(_file_config().get("sticker_dir") or "").strip()
        if conf:
            return Path(conf).expanduser()
    except Exception:
        pass
    return Path.home() / ".tg-rich-mcp-stickers"


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(fallback)) else fallback
    except Exception:
        return fallback


def _write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """原子写：写临时文件再替换，半截 JSON 永远不落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def load_library() -> list[dict[str, Any]]:
    data = _read_json(sticker_dir() / "library.json", {})
    stickers = data.get("stickers") if isinstance(data, dict) else None
    return stickers if isinstance(stickers, list) else []


def save_library(stickers: list[dict[str, Any]]) -> None:
    _write_json(sticker_dir() / "library.json", {"stickers": stickers}, mode=0o644)


def key_index(stickers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """emoji 键（主 emoji + emojis[] 别名，剥掉变体选择符）→ 贴纸池。"""
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in stickers:
        keys = [str(entry.get("emoji") or "")] + [
            str(e) for e in entry.get("emojis") or [] if isinstance(e, str)
        ]
        for key in keys:
            key = _strip_vs(key.strip())
            if key:
                index.setdefault(key, []).append(entry)
    return index


def _bot_id(token: str) -> str:
    """token 冒号前那截是 bot 的公开数字 id，不是秘密——拿它当缓存文件名。"""
    head = token.split(":", 1)[0]
    return head if head.isdigit() else "default"


def _cache_path(token: str) -> Path:
    return sticker_dir() / f"file-ids.{_bot_id(token)}.json"


def load_cache(token: str) -> dict[str, str]:
    return _read_json(_cache_path(token), {})


def remember_file_id(token: str, unique_id: str, file_id: str) -> None:
    cache = load_cache(token)
    cache[str(unique_id)] = str(file_id)
    _write_json(_cache_path(token), cache)


# ---------- 挑选 ----------
def resolve_emoji(content: str, index: dict[str, list[dict[str, Any]]]
                  ) -> tuple[list[dict[str, Any]], str] | None:
    """emoji 串 → (候选池, 归一化组合键)。认不出 / 交集为空 ⇒ None。

    整串优先查表（ZWJ 组合字不散架），查不到才按字素簇拆、逐个查、取交集。
    组合键按码点排序归一：（📖😭）和（😭📖）共用同一条「上次发过哪张」的记忆。
    """
    norm = _strip_vs(content.strip())
    if not norm or _looks_textual(norm):
        return None
    if norm in index:
        return list(index[norm]), norm
    parts = split_clusters(norm)
    if len(parts) < 2:
        return None
    pools: list[list[dict[str, Any]]] = []
    for part in parts:
        pool = index.get(part)
        if not pool:
            return None   # 有一个不在库里 ⇒ 整个标记不认
        pools.append(pool)
    ids = {str(e.get("id")) for e in pools[0]}
    for pool in pools[1:]:
        ids &= {str(e.get("id")) for e in pool}
    if not ids:
        return None       # 交集为空 ⇒ 想说的那张库里没有，不硬找
    combo = "".join(sorted(set(parts)))
    return [e for e in pools[0] if str(e.get("id")) in ids], combo


def pick(pool: list[dict[str, Any]], combo_key: str) -> dict[str, Any]:
    """池里随机挑一张，避开这个组合键上次刚发的那张（脸自动有变化）。"""
    state_path = sticker_dir() / "state.json"
    state = _read_json(state_path, {})
    last = state.get(combo_key)
    candidates = [e for e in pool if str(e.get("file_unique_id")) != str(last)]
    entry = random.choice(candidates or pool)
    state[combo_key] = str(entry.get("file_unique_id"))
    _write_json(state_path, state)
    return entry


# ---------- 发送（懒迁移） ----------
def _archive_file(entry: dict[str, Any]) -> Path:
    """归档原图必须真实住在库目录树内（符号链接按真实目标查）、扩展名合法。"""
    base = sticker_dir().resolve()
    real = (sticker_dir() / str(entry.get("file") or "")).resolve()
    if not str(real).startswith(str(base) + os.sep):
        raise RuntimeError(f"贴纸 {entry.get('id')} 的归档原图不在库目录内，拒发")
    if real.suffix.lower() not in STICKER_EXTS:
        raise RuntimeError(f"贴纸 {entry.get('id')} 的归档不是贴纸格式：{real.suffix}")
    if not real.is_file():
        raise RuntimeError(
            f"贴纸 {entry.get('id')}「{entry.get('title')}」的归档原图不见了：{real.name}"
        )
    return real


def send_entry(entry: dict[str, Any], chat_id: str, token: str,
               api: Callable[..., dict[str, Any]]) -> str:
    """v3 懒迁移：本 bot 缓存 → 400 才重传归档原图 → 新 file_id 写回缓存。"""
    unique = str(entry.get("file_unique_id") or "")
    cached = load_cache(token).get(unique) if unique else None
    if cached:
        try:
            api("sendSticker", {"chat_id": chat_id, "sticker": cached})
            return f"贴纸已发：{entry.get('title')}（{entry.get('emoji')}）"
        except ApiRejected as exc:
            if exc.code != 400:
                raise
            # 400 ＝ 这个 file_id 对本 bot 失效（换过 token 等），走重传
    real = _archive_file(entry)
    payload = api("sendSticker", {"chat_id": chat_id},
                  files={"sticker": (real.name, real.read_bytes())})
    result = payload.get("result") or {}
    fresh = ((result.get("sticker") or {}).get("file_id")
             if isinstance(result, dict) else None)
    if unique and fresh:
        remember_file_id(token, unique, str(fresh))
    return (f"贴纸已发：{entry.get('title')}（{entry.get('emoji')}）"
            "（本 bot 首次用这张，已从归档上传并缓存 file_id）")


# ---------- 工具：tg_sticker_send ----------
def _listing(stickers: list[dict[str, Any]]) -> str:
    if not stickers:
        return ("贴纸库还是空的。收到贴纸时用 tg_sticker_import 入库"
                "（agent 自己看图起标题、配 emoji 标签）。")
    lines = []
    for e in stickers:
        aliases = "".join(str(a) for a in e.get("emojis") or [])
        tags = "/".join(str(t) for t in e.get("tags") or [])
        lines.append(f"  {e.get('id')}. {e.get('emoji')}{aliases} {e.get('title')}"
                     + (f"（{tags}）" if tags else ""))
    return (f"馆藏 {len(stickers)} 张：\n" + "\n".join(lines)
            + "\n发送：emoji 挑张（多个 emoji＝交集收窄）／ id 直取 ／ query 按词搜。")


def tool_sticker_send(args: dict[str, Any], chat_id: str, token: str,
                      api: Callable[..., dict[str, Any]]) -> str:
    stickers = load_library()
    emoji = str(args.get("emoji") or "").strip()
    query = str(args.get("query") or "").strip()
    raw_id = str(args.get("id") if args.get("id") is not None else "").strip()

    if not emoji and not query and not raw_id:
        return _listing(stickers)
    if not stickers:
        return _listing(stickers)

    if raw_id:
        if not raw_id.isdigit():
            raise ValueError("id 必须是馆藏编号（先不带参数调一次看清单）")
        matches = [e for e in stickers if str(e.get("id")) == raw_id]
        if not matches:
            raise ValueError(f"馆藏里没有 {raw_id} 号（先不带参数调一次看清单）")
        return send_entry(matches[0], chat_id, token, api)

    if emoji:
        resolved = resolve_emoji(emoji, key_index(stickers))
        if resolved is None:
            raise ValueError(
                f"emoji「{emoji}」在库里没命中（不在库/交集为空）。"
                "不硬找——发错脸比不发更糟。先不带参数调一次看馆藏。"
            )
        pool, combo = resolved
        return send_entry(pick(pool, combo), chat_id, token, api)

    needle = query.lower()
    matches = [e for e in stickers if needle in " ".join(
        [str(e.get("title") or ""), str(e.get("desc") or "")]
        + [str(t) for t in e.get("tags") or []]).lower()]
    if not matches:
        raise ValueError(f"query「{query}」没搜到（搜的是标题/描述/标签）。"
                         "先不带参数调一次看馆藏。")
    return send_entry(pick(matches, f"q:{needle}"), chat_id, token, api)


# ---------- 工具：tg_sticker_import ----------
def _pending_dir() -> Path:
    return sticker_dir() / "pending"


def _next_id(stickers: list[dict[str, Any]]) -> int:
    return max((int(e.get("id", -1)) for e in stickers), default=-1) + 1


def _find_by_unique(stickers: list[dict[str, Any]], unique: str) -> dict[str, Any] | None:
    for e in stickers:
        if str(e.get("file_unique_id")) == unique:
            return e
    return None


def _download_original(file_id: str, api: Callable[..., dict[str, Any]],
                       download: Callable[[str], bytes]) -> tuple[str, str, bytes]:
    """getFile → 下载原图。返回 (file_unique_id, 扩展名, 字节)。"""
    payload = api("getFile", {"file_id": file_id})
    info = payload.get("result") or {}
    unique = str(info.get("file_unique_id") or "")
    remote = str(info.get("file_path") or "")
    size = int(info.get("file_size") or 0)
    if not unique or not remote:
        raise RuntimeError("getFile 的返回缺 file_unique_id/file_path，没法归档")
    ext = Path(remote).suffix.lower() or ".webp"
    if ext not in STICKER_EXTS:
        raise ValueError(f"这不像贴纸（{ext}）——本工具只收 {'/'.join(STICKER_EXTS)}")
    if size > STICKER_MAX_BYTES:
        raise ValueError(f"文件 {size / 1024 / 1024:.1f}MB，超过贴纸车道的 10MB 上限")
    return unique, ext, download(remote)


def tool_sticker_import(args: dict[str, Any], token: str,
                        api: Callable[..., dict[str, Any]],
                        download: Callable[[str], bytes]) -> str:
    file_id = str(args.get("file_id") or "").strip()
    unique_arg = str(args.get("file_unique_id") or "").strip()
    title = str(args.get("title") or "").strip()
    emoji = _strip_vs(str(args.get("emoji") or "").strip())
    stickers = load_library()

    # ---- 认领待认领区（不用重新下载） ----
    pending_record: dict[str, Any] | None = None
    if unique_arg and not file_id:
        pending_path = _pending_dir() / f"{unique_arg}.json"
        pending_record = _read_json(pending_path, {}) or None
        if not pending_record:
            existing = _find_by_unique(stickers, unique_arg)
            if existing:
                return (f"这张早就是馆藏 {existing.get('id')} 号"
                        f"「{existing.get('title')}」了，不用重复入库。")
            raise ValueError(
                f"待认领区里没有 {unique_arg}。新贴纸请带 file_id 来（我去下载归档）。")

    if pending_record:
        unique = unique_arg
        archive = Path(str(pending_record.get("file") or ""))
        file_id = str(pending_record.get("file_id") or "")
        if not archive.is_file():
            raise RuntimeError("待认领记录在、原图不在了——带 file_id 重新导一次")
    else:
        if not file_id:
            raise ValueError("要么给新贴纸的 file_id，要么给待认领区里的 file_unique_id")
        unique, ext, blob = _download_original(file_id, api, download)
        existing = _find_by_unique(stickers, unique)
        if existing:
            remember_file_id(token, unique, file_id)
            return (f"认识：馆藏 {existing.get('id')} 号「{existing.get('title')}」"
                    f"{existing.get('emoji')}（file_id 已更新进本 bot 缓存）。")
        _pending_dir().mkdir(parents=True, exist_ok=True)
        archive = _pending_dir() / f"{unique}{ext}"
        archive.write_bytes(blob)

    # ---- 元数据不全 ⇒ 落待认领区，等 agent 看图再来 ----
    if not (title and emoji):
        record = _read_json(_pending_dir() / f"{unique}.json", {})
        record.update({
            "file_id": file_id,
            "file_unique_id": unique,
            "file": str(archive),
            "emoji_hint": str(args.get("emoji_hint") or record.get("emoji_hint") or ""),
            "seen": int(record.get("seen") or 0) + 1,
        })
        _write_json(_pending_dir() / f"{unique}.json", record)
        return (
            f"已落待认领区（第 {record['seen']} 次见到它）。原图在：\n  {archive}\n"
            "下一步：用 Read 看这张图，然后再调一次 tg_sticker_import 带上\n"
            f"  file_unique_id=\"{unique}\" + title（起个名）+ emoji（主情绪）\n"
            "  可选 emojis（别名入口，越多越容易命中）/ tags / desc。\n"
            "归档自动化了，审美别自动化——标签要看着图写。"
        )

    # ---- 认领入库 ----
    new_id = _next_id(stickers)
    img_dir = sticker_dir() / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    final = img_dir / f"{new_id:03d}{archive.suffix.lower()}"
    final.write_bytes(archive.read_bytes())
    entry = {
        "id": new_id,
        "title": title,
        "desc": str(args.get("desc") or ""),
        "tags": [str(t) for t in args.get("tags") or [] if str(t).strip()],
        "emoji": emoji,
        "emojis": [_strip_vs(str(e)) for e in args.get("emojis") or [] if str(e).strip()],
        "file": str(final.relative_to(sticker_dir())),
        "file_unique_id": unique,
    }
    stickers.append(entry)
    save_library(stickers)
    if file_id:
        remember_file_id(token, unique, file_id)
    pending_json = _pending_dir() / f"{unique}.json"
    for leftover in (pending_json, *(p for p in [archive] if p.parent == _pending_dir())):
        try:
            leftover.unlink()
        except OSError:
            pass
    aliases = "".join(entry["emojis"])
    return (f"入库：馆藏 {new_id} 号「{title}」{emoji}{aliases}，原图归档 {entry['file']}。"
            f"现在（{emoji}）这类标记和 tg_sticker_send 都能命中它了。")


# ---------- 句内标记（渲染器模式的第二层） ----------
_MARKER_RE = re.compile(r"[（(]([^（）()\n]{1,32})[）)]")
_FENCE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def marker_max() -> int:
    raw = (os.environ.get("TG_STICKER_MAX") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_MARKER_MAX
    except ValueError:
        return DEFAULT_MARKER_MAX


def markers_enabled() -> bool:
    return (os.environ.get("TG_STICKER_MARKERS") or "1").strip() != "0"


def split_message(text: str) -> list[tuple[str, Any]]:
    """把正文按贴纸标记切开：[("text", 段), ("sticker", (池, 组合键, 原文)), …]。

    位置即语义：标记写在哪儿，贴纸就跟在哪条后面。
    认不出的标记原样留在正文（坏掉的时候最好看：一对普通括号，不穿帮）。
    反引号里的不算数——讨论这套语法本身时不会当场喷贴纸。
    """
    if not markers_enabled():
        return [("text", text)]
    stickers = load_library()
    if not stickers:
        return [("text", text)]
    index = key_index(stickers)

    masked = set()
    for m in _FENCE_RE.finditer(text):
        masked.update(range(m.start(), m.end()))

    parts: list[tuple[str, Any]] = []
    cursor = 0
    used = 0
    limit = marker_max()
    for m in _MARKER_RE.finditer(text):
        if used >= limit:
            break
        if m.start() in masked:
            continue
        resolved = resolve_emoji(m.group(1), index)
        if resolved is None:
            continue
        pool, combo = resolved
        chunk = text[cursor:m.start()].strip()
        if chunk:
            parts.append(("text", chunk))
        parts.append(("sticker", (pool, combo, m.group(0))))
        cursor = m.end()
        used += 1
    tail = text[cursor:].strip()
    if tail:
        parts.append(("text", tail))
    return parts or [("text", "")]
