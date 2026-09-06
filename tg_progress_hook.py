#!/usr/bin/env python3
"""进度窗 hook —— 把 agent 干活的过程一行一行推进 Telegram。

在手机上看着 agent 一步步干活：

    ┌ 正在干活…
    │ 📖 Read · server.py
    │ 🔍 Grep · handleRequest
    │ ⚡ Bash · 跑一遍测试
    └ 已经做了 12 步

⚠️ **这个文件是 Claude Code 专用的**（靠它的 PreToolUse hook 机制）。
   同目录的 tg_rich_mcp.py 是通用 MCP server，走握手式 MCP 的 host 都能用；
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
  * **draft（流式草稿）**：`sendRichMessageDraft` 逐帧动画，好看，
    **收工自动消失**（草稿不进聊天记录，正好是进度窗想要的结局）。
    ⚠️ **安卓代价**：草稿活跃期间 Telegram Android 把发送键换成省略号，
    用户发不出消息，**且这期间在输入框里打的字会在恢复时被清空**。
    官方记录 <https://bugs.telegram.org/c/62189> 被关闭称"预期行为"；
    Bot API 10.3 起本 hook 的每帧都带 `can_stop`——**新客户端**有停止按钮，
    按停＝草稿消失+输入框解锁，后续帧被客户端扔掉（2026-09-04 Android 实测）。
    但旧客户端**不画这颗按钮**、锁死照旧，且 hook 收不到 stop 事件（它走收信侧）。
    你没法预知用户拿的是哪版客户端 ⇒ 默认仍不走这条路，想要动画质感的自己打开。
    桌面端据用户反馈不锁输入框（用户反馈，不是官方的跨平台保证）。

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

import errno
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
# Stop 收尾抢推送锁的**有界**上限（R5）。它比宿主给 Stop 的超时（README 15s）大，
# 正常情况下在飞的编辑早就返回、这里立刻拿到锁把窗收掉；真被长请求卡住时，宿主会
# 先杀掉 Stop——而清理责任已在第一步落进 state 的 pending_cleanup，后续执行者（下一
# 轮 push、或下一次 Stop）持同一把推送锁时补做，绝不会因为这次没抢到就永远丢掉。
# 这个界只是防「宿主没配超时」时无限期挂住，不是保证清理在本次做完。
_FINISH_PUSH_WAIT_SECONDS = 45.0
_PUSH_SPIN_SECONDS = 0.05
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

# 副本的**字面量**必须单独拿出来：写在 except 里的话，测试环境下 import 总是成功，
# 比对的就变成"共享对象 == 共享对象"（永真），副本改坏了也没人知道。
_FALLBACK_TELEGRAM_TOKEN_PATTERN = r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}"

try:
    from secret_redaction import TELEGRAM_BOT_TOKEN_RE as _TELEGRAM_TOKEN_RE
except Exception:      # noqa: BLE001
    # 铁律①：少一个文件也不能让 hook 崩掉工具调用。
    _TELEGRAM_TOKEN_RE = re.compile(_FALLBACK_TELEGRAM_TOKEN_PATTERN)

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
    _TELEGRAM_TOKEN_RE,                               # Telegram bot token（共享真源）
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


def _amend_if_gen(state_path: Path, gen: int, patch: dict) -> bool:
    """代际栅栏版 _amend：只有 state 还停在同一代（gen 没变）才写，写了返回 True。

    锁管的是「别同时写」，管不了「这一轮已经结束了别再写」——Stop 收窗 /
    ROUND_GAP 开新轮都会把 gen +1，晚归的推送子进程据此发现自己写的是
    上一代的账，放弃登记（并把自己刚发出去的消息删掉，见 _push_locked）。
    """
    try:
        with open(str(state_path) + ".lock", "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                state = {}
            if int(state.get("gen") or 0) != int(gen):
                return False
            state.update(patch)
            _atomic_write(state_path, state)
            return True
    except Exception:
        return False


# 编辑/删除失败里，只有这一类拒收能证明"目标消息已不存在"，才该弃窗重开。
# 超时 / 429 / 5xx 都是临时的，不证明消息没了——清了 msg_id 就丢掉还能用的窗、
# 开一扇新的、旧窗失去登记、Stop 也收不掉它（B4）。文案信号全小写比对。
_MESSAGE_GONE_SIGNS = (
    "message to edit not found",
    "message to delete not found",
    "message can't be edited",
    "message can't be deleted",
    "message identifier is not specified",
    "message identifier is invalid",
    "message_id_invalid",
    "message not found",
)


def _looks_message_gone(exc: Exception) -> bool:
    """复用贴纸迁移的判据思路：先认错误码（只有 API 拒收才带 error_code=400），
    再在 400 里按文案收窄。拿不到 code（超时/网络错=普通 RuntimeError）一律不算没了。
    """
    if getattr(exc, "code", None) != 400:
        return False
    text = str(exc).lower()
    return any(sign in text for sign in _MESSAGE_GONE_SIGNS)


def _superseded(state: dict, seq: int) -> bool:
    """这一帧是否被更新的**已调度**推送顶替了？

    只认真派出过子进程的 seq（`sched_seq`）——被 1.2 秒节流、没派出子进程的新事件
    只推高了 `seq`，不能让唯一一个在飞的投递任务据此让位、最终零编辑（B6）。
    `sched_seq` 未设时回落到 `seq`（手工播种的老用例按原语义走）。
    """
    if not seq:
        return False
    sched = int(state.get("sched_seq") or state.get("seq") or 0)
    return seq < sched


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


# ---------- 持久化的收尾清理（R5）----------
# Stop 收窗时若同步等推送锁没等到（在飞的慢请求握着它、宿主又把 Stop 超时杀了），
# 光靠「同步删窗」会连责任一起丢：state 里 msg_id 已清零、又没别的地方记着「42 号窗
# 还没收」，那扇窗就永远留在聊天里、再也没人来删。做法是**清空活动窗口前先把清理
# 任务落进 state（pending_cleanup）**，由后续持同一把推送锁的执行者补做；同步那步只
# 是 best-effort、有界等待。
def _read_pending_cleanup(state_path: Path) -> dict | None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    task = state.get("pending_cleanup") if isinstance(state, dict) else None
    return task if isinstance(task, dict) and int(task.get("msg_id") or 0) else None


def _clear_pending_cleanup(state_path: Path) -> None:
    _amend(state_path, {"pending_cleanup": None})


def _run_cleanup_task(state_path: Path, task: dict) -> None:
    """执行一条持久化清理任务——**必须在持有推送锁时调用**（与普通推送同一把锁、
    同一套顺序，绝不和在飞的编辑抢同一扇窗）。

    做成（或服务器明确拒收＝目标已达成/无从达成）就把 pending_cleanup 清掉；
    只有**网络错/超时**才留着，让下一个执行者重试——绝不因一次网络抖动就把责任丢了，
    也绝不死循环重发一个服务器已经拒的请求。
    """
    from tg_rich_mcp import _default_chat, call_api  # noqa: PLC0415

    message_id = int(task.get("msg_id") or 0)
    if not message_id:
        _clear_pending_cleanup(state_path)
        return
    chat = _default_chat()
    if not chat:
        return   # 没 chat：先留着，下次再来
    lines = task.get("lines") or []
    total = int(task.get("total") or 0)
    end_mode = str(task.get("end_mode") or "delete")
    try:
        done = False
        if end_mode == "delete":
            try:
                call_api("deleteMessage", {"chat_id": chat, "message_id": message_id})
                done = True
            except Exception as exc:          # noqa: BLE001
                if getattr(exc, "code", None) is None:
                    return                    # 网络错（无 error_code）：留着重试
                # 服务器拒收（如超 48h 删不掉，带 error_code）：退回定格
        if not done:
            call_api("editMessageText", {
                "chat_id": chat, "message_id": message_id,
                "rich_message": _rich(_blocks(lines, total, done=True)),
            })
    except Exception as exc:                   # noqa: BLE001
        if getattr(exc, "code", None) is None:
            return                             # 网络错：留着重试
        # 服务器明确拒收（消息没了/不能编辑）：目标达成或无从达成，别死循环，往下清掉
    _clear_pending_cleanup(state_path)


def _drain_pending_cleanup(state_path: Path, wait_seconds: float) -> None:
    """有界地抢推送锁、把 pending_cleanup 补做掉；抢不到就留着给后续执行者。"""
    if not _read_pending_cleanup(state_path):
        return                                 # 没有待清任务：连锁都不碰
    lock_path = Path(str(state_path) + ".push.lock")
    try:
        with open(str(lock_path), "w", encoding="utf-8") as lock_file:
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise
                if time.monotonic() >= deadline:
                    return                     # 有界超时：责任留在 state，后续执行者补做
                time.sleep(_PUSH_SPIN_SECONDS)
            task = _read_pending_cleanup(state_path)   # 锁内重读，别和别的执行者重复做
            if task:
                _run_cleanup_task(state_path, task)
    except Exception:
        pass                                   # 铁律①：收尾清理再失败也不许炸 Stop


def _push(state_path: Path, seq: int = 0) -> int:
    """子进程入口：把状态文件里的行推出去。失败静默。

    **整段持一把独立的推送锁。** 只在出发前看一眼 seq 是不够的——检查通过之后、
    网络请求返回之前，更新的那一帧可能已经发完了，这一帧再落地就把窗口改回了旧内容
    （用户眼里就是进度倒退）。加锁之后两条路都安全：旧帧先拿到锁就先发、新帧随后覆盖；
    新帧先拿到锁的话，旧帧拿到锁时**在锁内重读**状态，发现 seq 过期直接退出。

    锁必须**独立于主状态锁**：主锁是每次工具调用都要拿的，把网络请求塞进那把锁里，
    等于让 Telegram 的延迟拖住 agent 干活（铁律②）。
    """
    lock_path = Path(str(state_path) + ".push.lock")
    try:
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            _push_locked(state_path, seq)
    except Exception:
        pass          # 铁律①：推送失败绝不影响 agent
    return 0


def _push_locked(state_path: Path, seq: int = 0) -> int:
    """真正干活的那半——**必须在推送锁里调用**，状态也必须在锁内重读。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tg_rich_mcp import _default_chat, call_api, tool_draft  # noqa: PLC0415

        state = json.loads(state_path.read_text(encoding="utf-8"))
        lines = state.get("lines") or []
        total = int(state.get("total") or 0)
        gen = int(state.get("gen") or 0)   # 这一帧属于哪一代，登记时要对得上

        if _mode() == "draft":
            # 锁内重读之后再判：被更新的**已调度**帧顶替了才让位（那帧的子进程真在飞）。
            # 只被节流推高 seq、没派子进程的，不能让唯一在飞的投递任务让位（B6）。
            if _superseded(state, seq):
                return 0
            tool_draft({
                "draft_id": int(state.get("draft_id") or 0),
                "blocks": _blocks(lines, total, draft=True),
                # 新客户端由此得到停止按钮（按停＝解锁输入框）；旧客户端无感。
                # hook 收不到 stop 事件——按停后这儿会继续瞎推，但客户端会把
                # 已停 draft_id 的后续帧直接扔掉，无害。
                "can_stop": True,
            })
            return 0

        chat = _default_chat()
        if not chat:
            return 0

        # Stop 没做完的持久化清理（宿主超时/进程被杀留下的，R5）：这里正持着推送锁，
        # 与在飞的编辑同一套顺序，顺手把它补做掉。放在开新窗之前，免得旧窗和新窗并存。
        task = state.get("pending_cleanup")
        if isinstance(task, dict) and int(task.get("msg_id") or 0):
            _run_cleanup_task(state_path, task)
            state["pending_cleanup"] = None   # 本地副本同步清，避免本轮后续再看到

        # 上一轮的孤儿窗（Stop hook 没跑到、ROUND_GAP 顶替收的场）：开新窗前先收掉
        stale = int(state.get("stale_msg") or 0)
        if stale:
            try:
                call_api("deleteMessage", {"chat_id": chat, "message_id": stale})
            except Exception:
                pass                     # 超 48 小时删不掉就算了，别挡新窗
            _amend_if_gen(state_path, gen, {"stale_msg": 0})

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
                mid = int(result["message_id"])
                if not _amend_if_gen(state_path, gen, {"msg_id": mid}):
                    # 网络请求还在飞的时候这一轮已经被收掉了（Stop / 新轮）。
                    # 登记进去就是复活孤儿窗——不登记，并且把刚生出来的消息删掉。
                    try:
                        call_api("deleteMessage",
                                 {"chat_id": chat, "message_id": mid})
                    except Exception:
                        pass
            return 0

        if _superseded(state, seq):
            return 0
        try:
            call_api("editMessageText", {
                "chat_id": chat,
                "message_id": message_id,
                "rich_message": _rich(_blocks(lines, total)),
            })
        except Exception as exc:
            # 只有**确认目标消息已不存在**的拒收才弃窗（松开手、下一帧重开一扇）。
            # 超时/429/5xx 都不证明 42 被删——保留 msg_id、下一帧照常重试同一扇窗，
            # 不然临时抖一下就丢窗、开新窗、旧窗失登记 Stop 收不掉（B4）。
            # 弃窗也要过栅栏——这一轮已经结束的话，别把新一轮的 claim 清成孤儿。
            if _looks_message_gone(exc):
                _amend_if_gen(state_path, gen,
                              {"msg_id": 0, "claim": 0, "claim_at": 0.0})
    except Exception:
        pass          # 铁律①：推送失败绝不影响 agent
    return 0


def _finish() -> int:
    """Stop hook 入口：解析 stdin 后交给 _finish_session（拆开是为了能被测试直调）。"""
    try:
        event = json.loads(sys.stdin.read(STDIN_LIMIT) or "{}")
        session = str(event.get("session_id") or "default")
    except Exception:
        return 0
    return _finish_session(session)


def _finish_session(session: str) -> int:
    """收工——默认把窗口撤掉，`keep` 时定格成终态。之后下一轮另开一扇。

    先关账、后善后：**第一步在 state 锁里一笔完成「gen+1 + 摘走 msg_id + 清空
    + 把该 msg_id 的清理责任落进 pending_cleanup」**，从这一刻起本轮就算死了——
    还在飞的推送子进程回来后 CAS(gen) 必然失败，会自己把刚发出去的消息删掉
    （见 _push_locked），不会再有孤儿窗复活。

    第二步做 best-effort 的即时清理，**有界**等推送锁（`_FINISH_PUSH_WAIT_SECONDS`）：
    ⚠️ 注意这一步**可能被 Telegram 的延迟拖住**（在飞的编辑握着推送锁时最多等到上界），
    但即使宿主把 Stop 超时杀了、或这步没抢到锁，清理责任已在第一步落进 state，
    后续持同一把推送锁的执行者（下一轮 push / 下一次 Stop）会补做——责任不会随进程退出丢失。
    （这条与铁律②「不拖慢工具调用」不冲突：Stop 不是工具调用前置钩子，关账那步没被网络
    拖慢；这里拖住的只是 Stop 自己的收尾，且有界、有兜底。）
    """
    state_path = _state_path(session)
    # —— 第一步：锁内关账（bump gen + 摘走 msg_id + 清空 + 落 pending_cleanup）——
    try:
        with open(str(state_path) + ".lock", "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except Exception:
                state = {}
            message_id = int(state.get("msg_id") or 0)
            lines = state.get("lines") or []
            total = int(state.get("total") or 0)
            # 关账要**同时废掉旧 seq**：光 bump gen 挡不住排队/在飞的推送——draft 路径
            # 只比 seq，Stop 后排队的子进程读到同一个 seq 仍会喷一帧"正在干活/0步"（B5）。
            # seq 与 sched_seq 一起推高 → 任何旧 seq 的推送经 _superseded 让位。
            new_seq = int(state.get("seq") or 0) + 1
            patch = {
                "gen": int(state.get("gen") or 0) + 1,
                "seq": new_seq, "sched_seq": new_seq,
                "msg_id": 0, "claim": 0, "claim_at": 0.0,
                "lines": [], "total": 0, "last_push": 0.0,
            }
            # **清空活动窗口之前**把清理责任落盘（R5）：只认 msg_id 不认当前模式——
            # 用户 edit 跑了半截、重启改成 draft 时，账上挂着的持久窗照样要收掉。
            if message_id:
                patch["pending_cleanup"] = {
                    "msg_id": message_id, "lines": lines, "total": total,
                    "end_mode": _end_mode(),
                }
            state.update(patch)
            _atomic_write(state_path, state)
    except Exception:
        return 0

    # —— 第二步：best-effort 即时清理，有界等推送锁；抢不到/被杀都有 pending_cleanup 兜底。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    _drain_pending_cleanup(state_path, _FINISH_PUSH_WAIT_SECONDS)
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
            # seq 接着往上走，别让在途的旧子进程撞上新号；gen 也 +1——这是和
            # _finish_session 同一套代际栅栏，旧轮在飞的推送不得再登记进新轮。
            # 旧轮账上还挂着的窗（Stop 没跑到留下的）转进 stale_msg，
            # 下一个推送子进程开新窗前顺手收掉（网络活不进 hook 主体，铁律②）。
            if now - float(state.get("last_push") or 0) > ROUND_GAP:
                stale_msg = (int(state.get("msg_id") or 0)
                             if _end_mode() == "delete" else 0)
                state = {"seq": int(state.get("seq") or 0),
                         "gen": int(state.get("gen") or 0) + 1,
                         "stale_msg": stale_msg}

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
                # 记下"真被调度出去的那个 seq"：只有它能让更旧的在飞帧让位。
                # 被节流没派子进程的事件只推高 seq、不动 sched_seq（B6）。
                state["sched_seq"] = state["seq"]
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
