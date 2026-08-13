#!/usr/bin/env python3
"""入站贴纸识别 hook（可选，仅 Claude Code：挂 UserPromptSubmit）。

用户发来贴纸时，channel 消息里只有 file_id——认不认识它本来要靠 agent
「记得去查」。这个 hook 把这份注意力税收掉：

- **认识的**（在贴纸库里）→ 注入标题/emoji/标签/描述，agent 不用下载看图，
  秒懂这张脸什么意思；
- **不认识的** → 注入一行提醒，file_id 已经带好，agent 得空调一次
  tg_sticker_import 就归档。

三条铁律同进度窗 hook：
1. **永不阻断**——任何异常吞掉，永远 exit 0；
2. **不拖慢**——**零网络**（识别只查本地库；下载归档留给 tg_sticker_import）；
3. **不泄密**——只回显消息里本来就有的 file_id 和库里自己写的文字。

挂法（.claude/settings.json，改完重开会话生效）：

    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python3 /绝对路径/tg_sticker_hook.py", "timeout": 5}]}]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_LINES = 3   # 一轮最多注入几条（同轮塞十张贴纸的场景不值得刷屏）

# 真 file_id 的形状：20 位以上、只含 base64url 字符。挡住文档示例、
# 测试模板（attachment_file_id="{fid}"）这类长得像 tag 的假货。
_PLAUSIBLE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_TAG_RE = re.compile(r"<channel\b[^>]*attachment_kind=\"sticker\"[^>]*>")


def _attr(tag: str, name: str) -> str:
    m = re.search(name + r'="([^"]*)"', tag)
    return m.group(1) if m else ""


def _reverse_file_id_map() -> dict[str, str]:
    """所有 bot 缓存反转：file_id → file_unique_id。

    入站 tag 通常只有 file_id；同一 bot 对同一张贴纸的 file_id 稳定，
    所以 import 时记下的那份就能认出后续入站。
    """
    import tg_sticker
    out: dict[str, str] = {}
    for cache in tg_sticker.sticker_dir().glob("file-ids.*.json"):
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            for unique, fid in data.items():
                out[str(fid)] = str(unique)
    return out


def scan(prompt: str) -> str:
    import tg_sticker
    stickers = tg_sticker.load_library()
    by_unique = {str(e.get("file_unique_id")): e for e in stickers}
    fid_to_unique: dict[str, str] | None = None   # 懒建，多数轮根本没贴纸

    lines: list[str] = []
    for m in _TAG_RE.finditer(prompt):
        if len(lines) >= MAX_LINES:
            break
        tag = m.group(0)
        fid = _attr(tag, "attachment_file_id")
        unique = _attr(tag, "attachment_unique_id")
        if not _PLAUSIBLE_ID.match(fid):
            continue
        entry = by_unique.get(unique) if unique else None
        if entry is None:
            if fid_to_unique is None:
                fid_to_unique = _reverse_file_id_map()
            entry = by_unique.get(fid_to_unique.get(fid, ""))
        if entry is not None:
            aliases = "".join(str(a) for a in entry.get("emojis") or [])
            tags = "/".join(str(t) for t in entry.get("tags") or [])
            desc = str(entry.get("desc") or "")
            lines.append(
                f"【贴纸识别·tg-rich】馆藏 {entry.get('id')} 号"
                f"「{entry.get('title')}」{entry.get('emoji')}{aliases}"
                + (f"（{tags}）" if tags else "")
                + (f"——{desc}" if desc else "")
                + "，不用下载看图。"
            )
        else:
            lines.append(
                "【贴纸·tg-rich】没见过这张。得空归档："
                f'tg_sticker_import(file_id="{fid}")，看图起标题配 emoji 后认领。'
            )
    return "\n".join(lines)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        prompt = str(payload.get("prompt") or "")
        if "attachment_kind" in prompt:   # 快路径：绝大多数轮零成本掠过
            out = scan(prompt)
            if out:
                print(out)
    except Exception:
        pass   # 铁律一：识别挂了最多少一行提示，绝不能挡用户说话
    sys.exit(0)


if __name__ == "__main__":
    main()
