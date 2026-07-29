#!/usr/bin/env python3
"""进度窗 hook —— 把 agent 干活的过程一行一行推进 Telegram。

在手机上看着 agent 一步步干活：

    ┌ 正在干活…
    │ 📖 Read · server.py
    │ 🔍 Grep · handleRequest
    │ ⚡ Bash · 跑一遍测试
    └ 已经做了 12 步

⚠️ **这个文件是 Claude Code 专用的**（靠它的 PreToolUse hook 机制）。
   同目录的 tg_rich_mcp.py 是通用 MCP server，任何 MCP host 都能用；
   这个 hook 搬不到别的 host——它们没有"工具调用前"这个钩子。

挂法：写进项目或用户的 .claude/settings.json
    "hooks": {
      "PreToolUse": [{"hooks": [{"type": "command",
        "command": "python3 /绝对路径/tg_progress_hook.py", "timeout": 5}]}],
      "Stop": [{"hooks": [{"type": "command",
        "command": "python3 /绝对路径/tg_progress_hook.py --finish", "timeout": 15}]}]
    }
  PreToolUse 那条是每一帧，Stop 那条是收工。只挂前一条也能用，
  只是窗口会停在最后一帧、不会自己收拾。

两种形态（`TG_PROGRESS_MODE`，默认 `edit`）：

  * **edit（默认·持久窗）**：开工发一条正式富消息，之后每帧 `editMessageText`
    原地改它。不受草稿 30 秒寿命限制、留在聊天记录里、编辑不响铃。
  * **draft（流式草稿）**：`sendRichMessageDraft` 逐帧动画，好看。
    ⚠️ **安卓代价**：草稿活跃期间 Telegram Android 把发送键换成省略号，
    用户发不出消息，**且这期间在输入框里打的字会在恢复时被清空**。
    官方记录 <https://bugs.telegram.org/c/62189>，Telegram 已关闭该 issue
    并称是"当前预期行为"——所以这不是等一个修复就能好的事。
    长任务里用户最需要插话的时刻（补条件、喊停、纠方向）恰好是草稿最活跃的时刻，
    所以默认不走这条路；桌面端据用户反馈不锁输入框（用户反馈，不是官方的跨平台
    保证），想要动画质感的自己打开。

收工怎么处置（`TG_PROGRESS_END`，默认 `delete`）：干完活把那扇窗**撤掉**，
聊天记录里一条工具调用都不留。`keep` 则定格成终态留档。删不掉（超 48 小时 /
已被手删）自动退回定格。

配置：读 ~/.tg-rich-mcp.json（和 MCP server 同一份）。
  hook 是编辑器另起的进程，**拿不到你写在 MCP server 那段 env 里的变量**，
  所以这里必须用配置文件（或者把变量 export 进编辑器的启动环境）。

三条铁律，改的时候别破：
  1. **永不阻断工具调用**——任何异常都吞掉，永远 exit 0。
     推送失败最多让窗口不动，绝不能让 agent 干不了活。
  2. **不拖慢工具调用**——网络请求丢给 detached 子进程，主体只写状态就退。
     hook 是串在每次工具调用前面的，多花的每毫秒都乘以调用次数。
  3. **不泄密**——只推工具名 + 一句短摘要，且摘要要过**两道闸**：
     关键词闸（说出"密钥"两个字）+ **形态闸**（长得像密钥）。
     只有关键词闸是不够的——`deploy sk-live-ABC123XYZ` 一个关键词都没有。

关掉：设环境变量 TG_PROGRESS=0
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".tg-progress"
MIN_INTERVAL = 1.2      # 秒，两次推送之间的最小间隔（别把 Bot API 打爆）
MAX_LINES = 8           # 窗口里最多显示的最近行数
SUMMARY_LIMIT = 48      # 摘要截断长度
STDIN_LIMIT = 512 * 1024   # 读 stdin 的上限，别让超大 tool_input 拖住主体
ROUND_GAP = 150.0       # 秒，离上一帧这么久没动静就当新一轮，另开一扇窗
CLAIM_TTL = 60.0        # 秒，开窗这活派出去多久还没拿到 message_id 就允许改派
TITLE = os.environ.get("TG_PROGRESS_TITLE", "正在干活…")
DONE_TITLE = os.environ.get("TG_PROGRESS_DONE_TITLE", "干完了")


def _mode() -> str:
    """`edit`=持久窗（默认）；`draft`=流式草稿（会锁安卓输入框，显式打开才走）。"""
    mode = os.environ.get("TG_PROGRESS_MODE", "edit").strip().lower()
    return mode if mode in ("edit", "draft") else "edit"


def _end_mode() -> str:
    """收工怎么处置那扇窗：`delete`（默认，撤掉）或 `keep`（定格成终态留档）。"""
    return "keep" if os.environ.get("TG_PROGRESS_END", "delete").strip().lower() in (
        "keep", "final", "1", "true", "yes",
    ) else "delete"

# 闸一：关键词。摘要里出现这些词就整条隐去。
DIRTY = (
    "token", "secret", "password", "passwd", "credential", "api_key", "apikey",
    "private_key", "authorization", ".env", "id_rsa", "cookie", "session_key",
)

# 闸二：形态。关键词闸只拦「说出'密钥'两个字」，拦不住密钥本身——
# `deploy sk-live-ABC123XYZ`、文件名 `AKIAIOSFODNN7EXAMPLE` 都没有任何关键词。
# 这道闸认长相，不认词。
SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),              # OpenAI 及仿它的
    re.compile(r"AKIA[0-9A-Z]{12,}"),                 # AWS access key
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),        # GitHub
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),      # Slack
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\."),           # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),     # PEM
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}"),       # Telegram bot token
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),              # 长 hex
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}"),        # 长 base64
    re.compile(r"://[^/\s:]+:[^/\s@]+@"),             # URL 里的 user:pass@
)

# Grep/Glob 的 pattern 天生就是「在找什么值」，是最可能直接含密钥的地方。
# 只放行长得像标识符/路径的短模式，其余一律不发。
SAFE_PATTERN = re.compile(r"^[\w一-鿿 .*/_\-]{1,24}$")

ICONS = {
    "Read": "📖", "Edit": "✏️", "Write": "📝", "NotebookEdit": "📝",
    "Bash": "⚡", "Grep": "🔍", "Glob": "🔍", "WebSearch": "🌐", "WebFetch": "🌐",
    "Task": "🧩", "Agent": "🧩", "TodoWrite": "🗒️",
}


def _redact_on() -> bool:
    """闸的总开关。

    `TG_PROGRESS_REDACT=0` 关掉，摘要原样推。有人就是想在窗口里看到完整命令，
    那是他自己的机器和他自己的选择——**但关了以后**，Bash 的说明、文件名、搜索词
    会原样发进 Telegram，密钥形态不再拦。想清楚再关。
    """
    return os.environ.get("TG_PROGRESS_REDACT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _looks_secret(text: str) -> bool:
    return any(shape.search(text) for shape in SECRET_SHAPES)


def _note_redacted(kind: str, tool: str, length: int) -> None:
    """记一笔「这儿被隐过」，落在 ~/.tg-progress/redacted.log。

    **刻意不记原文**——记了就等于把密钥抄进另一个文件，闸就白设了。
    只留时间、哪个工具、命中哪道闸、原摘要多长，够回头对账。
    沉默的安全机制是最危险的那种，所以宁可留一行没内容的账。
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = time.strftime("%m-%d %H:%M:%S")
        with open(STATE_DIR / "redacted.log", "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {tool or '?'} {kind} len={length}\n")
    except Exception:
        pass


def _clean(text: str, tool: str = "") -> str:
    text = " ".join(str(text).split())
    if not _redact_on():
        return text[:SUMMARY_LIMIT]
    hit = "keyword" if any(word in text.lower() for word in DIRTY) else (
        "shape" if _looks_secret(text) else "")
    if hit:
        _note_redacted(hit, tool, len(text))
        return "（内容隐去）"
    return text[:SUMMARY_LIMIT]


def _safe_url(raw: str, tool: str = "") -> str:
    """URL 只留 host + path。query 里常有签名/token，userinfo 里直接是账号密码。"""
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415

        parts = urlsplit(raw)
        if not parts.netloc:
            return _clean(raw, tool)
        return _clean(f"{parts.hostname or ''}{(parts.path or '')[:40]}", tool)
    except Exception:
        return ""


def _summarize(tool: str, payload: dict) -> str:
    """只从参数里取一句安全的短摘要，绝不取命令全文或文件内容。"""
    if not isinstance(payload, dict):
        return ""
    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        # 文件名本身也可能就是密钥（实测：Read 一个叫 AKIA… 的文件）
        return _clean(Path(str(payload.get("file_path") or "")).name, tool)
    if tool == "Bash":
        # 用 description（人话说明），不用 command——命令里可能带 token
        return _clean(payload.get("description") or "跑了一条命令", tool)
    if tool in ("Grep", "Glob"):
        pattern = " ".join(str(payload.get("pattern") or payload.get("glob") or "").split())
        if pattern and SAFE_PATTERN.match(pattern) and not _looks_secret(pattern):
            return pattern
        return ""          # 长得不像普通标识符就一个字都不说
    if tool == "WebFetch":
        return _safe_url(str(payload.get("url") or ""), tool)
    if tool == "WebSearch":
        return _clean(payload.get("query") or "", tool)
    if tool in ("Task", "Agent"):
        return _clean(payload.get("description") or "", tool)
    return ""


def _line(tool: str, payload: dict) -> str:
    tool = re.sub(r"[^\w.:-]", "", str(tool))[:40]      # 工具名也是外来输入
    icon = ICONS.get(tool, "🔧")
    if tool.startswith("mcp__"):
        icon = "🔌"
        tool = tool.split("__")[-1]
    summary = _summarize(tool, payload)
    return f"{icon} {tool} · {summary}" if summary else f"{icon} {tool}"


def _draft_id(session: str) -> int:
    """每个会话一个稳定的 draft_id——同一个 id 才会在客户端做动画过渡。"""
    digest = hashlib.md5(session.encode("utf-8", "replace")).hexdigest()[:7]
    return int(digest, 16) or 1


def _state_path(session: str) -> Path:
    """文件名只由哈希决定——session_id 是外来输入，直接拼进路径能用 `../` 跑出去写别处。"""
    return STATE_DIR / (hashlib.sha1(session.encode("utf-8", "replace")).hexdigest()[:20] + ".json")


def _atomic_write(path: Path, payload: dict) -> None:
    """先写同目录临时文件再 rename——推送子进程可能正在读，不能让它读到写了一半的。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _amend(state_path: Path, patch: dict) -> None:
    """给状态文件打个补丁（读—改—写全程持锁），推送子进程回写 message_id 用。"""
    try:
        with open(str(state_path) + ".lock", "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                state = {}
            state.update(patch)
            _atomic_write(state_path, state)
    except Exception:
        pass


def _blocks(lines: list[str], total: int, done: bool = False,
            draft: bool = False) -> list[dict]:
    # total 必须单独记：lines 只留最近 40 条，拿 len(lines) 当步数的话，
    # 第 41 步起就永远显示「已经做了 40 步」。
    shown = lines[-MAX_LINES:]
    blocks: list[dict] = [
        {"type": "heading", "size": 4, "text": DONE_TITLE if done else TITLE},
    ]
    if shown:
        blocks.append(
            {"type": "list",
             "items": [{"blocks": [{"type": "paragraph", "text": text}]} for text in shown]})
    tail = f"一共 {total} 步" if done else f"已经做了 {total} 步"
    # `thinking` 块**只有草稿能用**，官方明写它进不了正式消息——
    # 持久窗（send/edit）走 footer，照搬 thinking 会被 API 拒收。
    blocks.append({"type": "thinking", "text": tail} if draft
                  else {"type": "footer", "text": tail})
    return blocks


def _rich(blocks: list[dict]) -> str:
    return json.dumps({"blocks": blocks}, ensure_ascii=False)


def _push(state_path: Path, seq: int = 0) -> int:
    """子进程入口：把状态文件里的行推出去。失败静默。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tg_rich_mcp import _default_chat, call_api, tool_draft  # noqa: PLC0415

        state = json.loads(state_path.read_text(encoding="utf-8"))
        lines = state.get("lines") or []
        total = int(state.get("total") or 0)

        if _mode() == "draft":
            # 帧序：几个推送子进程会并发落地，慢的那个会把旧内容盖回去（窗口倒退）。
            # 出发时记下 seq，推之前再看一眼——已经有更新的帧了就让位，反正它马上到。
            if seq and int(state.get("seq") or 0) != seq:
                return 0
            tool_draft({
                "draft_id": int(state.get("draft_id") or 0),
                "blocks": _blocks(lines, total, draft=True),
            })
            return 0

        chat = _default_chat()
        if not chat:
            return 0
        message_id = int(state.get("msg_id") or 0)
        if not message_id:
            # 开窗只能有一个人干，否则并发的几帧会各发一条，聊天里冒出好几扇窗。
            # 主体在锁里派了活（claim=某个 seq），不是这帧就让开。
            if int(state.get("claim") or 0) != seq:
                return 0
            payload = call_api("sendRichMessage", {
                "chat_id": chat,
                "rich_message": _rich(_blocks(lines, total)),
                "disable_notification": "true",
            })
            result = payload.get("result")
            if isinstance(result, dict) and result.get("message_id"):
                _amend(state_path, {"msg_id": int(result["message_id"])})
            return 0

        if seq and int(state.get("seq") or 0) != seq:
            return 0
        try:
            call_api("editMessageText", {
                "chat_id": chat,
                "message_id": message_id,
                "rich_message": _rich(_blocks(lines, total)),
            })
        except Exception:
            # 那条消息没了（被删/被清），松开手，下一帧重新开一扇
            _amend(state_path, {"msg_id": 0, "claim": 0, "claim_at": 0.0})
    except Exception:
        pass          # 铁律①：推送失败绝不影响 agent
    return 0


def _finish() -> int:
    """Stop hook：收工——默认把窗口撤掉，`keep` 时定格成终态。之后下一轮另开一扇。"""
    try:
        event = json.loads(sys.stdin.read(STDIN_LIMIT) or "{}")
        session = str(event.get("session_id") or "default")
    except Exception:
        return 0

    state_path = _state_path(session)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tg_rich_mcp import _default_chat, call_api  # noqa: PLC0415

        state = json.loads(state_path.read_text(encoding="utf-8"))
        message_id = int(state.get("msg_id") or 0)
        chat = _default_chat()
        if message_id and chat and _mode() == "edit":
            gone = False
            if _end_mode() == "delete":
                try:
                    call_api("deleteMessage",
                             {"chat_id": chat, "message_id": message_id})
                    gone = True
                except Exception:
                    gone = False       # 超 48 小时删不掉，退回定格
            if not gone:
                call_api("editMessageText", {
                    "chat_id": chat,
                    "message_id": message_id,
                    "rich_message": _rich(_blocks(state.get("lines") or [],
                                                  int(state.get("total") or 0), done=True)),
                })
    except Exception:
        pass
    # 无论上面成没成，都把窗口交出去：下一轮从零开一扇新的
    _amend(state_path, {"msg_id": 0, "claim": 0, "claim_at": 0.0,
                        "lines": [], "total": 0, "last_push": 0.0})
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--finish":
        return _finish()
    if len(sys.argv) >= 3 and sys.argv[1] == "--push":
        return _push(Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 0)

    if os.environ.get("TG_PROGRESS", "1") not in ("1", "true", "yes"):
        return 0

    try:
        event = json.loads(sys.stdin.read(STDIN_LIMIT) or "{}")
        if not isinstance(event, dict):
            return 0
    except Exception:
        return 0

    tool = str(event.get("tool_name") or "")
    if not tool or tool == "TodoWrite":
        return 0

    # 别把推送自己也推出去，会自激。
    # 只扫顶层标量字段——整包 json.dumps 会把超大 tool_input 完整遍历一遍，白花时间。
    payload = event.get("tool_input") or {}
    if isinstance(payload, dict):
        probe = " ".join(
            str(value)[:200] for value in payload.values()
            if isinstance(value, (str, int, float))
        )
        if "tg_progress_hook" in probe:
            return 0

    session = str(event.get("session_id") or "default")
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = _state_path(session)

    # 读—改—写整段上锁：并行工具调用会同时进来，无锁时后写的覆盖先写的
    # （实测 100 个并发只剩 24 条）。锁 + 原子替换一起上，缺一个都不算修好。
    line = _line(tool, payload)
    try:
        with open(str(state_path) + ".lock", "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError
            except Exception:
                state = {}

            now = time.time()
            # 冷了这么久还来一帧，多半是新的一轮（Stop hook 没挂 / 没跑到的兜底）。
            # seq 接着往上走，别让在途的旧子进程撞上新号。
            if now - float(state.get("last_push") or 0) > ROUND_GAP:
                state = {"seq": int(state.get("seq") or 0)}

            state.setdefault("draft_id", _draft_id(session))
            if not isinstance(state.get("lines"), list):
                state["lines"] = []
            state["lines"].append(line)
            state["lines"] = state["lines"][-40:]
            state["total"] = int(state.get("total") or 0) + 1     # 真步数，不受上面截断影响
            state["seq"] = int(state.get("seq") or 0) + 1

            due = now - float(state.get("last_push") or 0) >= MIN_INTERVAL
            if due:
                state["last_push"] = now
                # 持久窗要先有一条消息才能改。派活给这一帧，超时未果再改派。
                if not int(state.get("msg_id") or 0) and (
                        not state.get("claim")
                        or now - float(state.get("claim_at") or 0) > CLAIM_TTL):
                    state["claim"] = state["seq"]
                    state["claim_at"] = now
            seq = state["seq"]
            _atomic_write(state_path, state)
    except Exception:
        return 0

    if due:
        # 铁律②：网络请求交给 detached 子进程，主体到这里就退
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()),
                 "--push", str(state_path), str(seq)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # 铁律①：永不阻断
