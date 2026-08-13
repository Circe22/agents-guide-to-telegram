#!/usr/bin/env python3
"""tg_ask —— 把问题变成 Telegram 按钮，在工具调用内**同步**等答案。

两个工具的机制层（注册在 tg_rich_mcp.py）：
  tg_ask_choice      发一道选择题 → 长轮询 getUpdates 等点击 → 返回选了哪个
  tg_ask_permission  权限审批卡：Claude Code `--permission-prompt-tool` 的
                     手机对话框（✅允许/❌拒绝；**超时＝拒绝，fail-closed**）

前提（README「按钮问答要专用 bot」）：**bot 必须归本 server 独用**。
getUpdates 同一时刻全 Telegram 只允许一个消费者——官方插件、webhook、
或另一个进程在场时 API 回 409，这里会明确报错而不是傻等。

布局规则来自真机四组对照实测（2026-08-14 定案）：
超过 16 字（中文计）的按钮文字会被 Telegram **像素级硬剪、连省略号都不给**，
所以选项全短才把文字放上按钮；任何一条超线，整题切成
「正文列选项全文 + 1️⃣2️⃣3️⃣ 编号按钮」——按钮变遥控器，文字住正文里。
"""

from __future__ import annotations

import json
import re
import secrets
import time
import unicodedata
from typing import Any, Callable

from secret_redaction import redact_telegram_tokens
from tg_progress_hook import DIRTY, SECRET_SHAPES
from tg_sticker import ApiRejected

MAX_OPTIONS = 20
MAX_COLUMNS = 5
BUTTON_WIDTH_LIMIT = 16.0   # 中文计：CJK 一个算 1 字，拉丁/数字算半字
LABEL_HARD_LIMIT = 300      # 连正文模式都装不下的选项，是题出错了
TEXT_LIMIT = 4096           # Bot API sendMessage 的正文上限
POLL_SLICE = 25             # 单次 getUpdates 长轮询秒数（要小于 call_api 的 45s）
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600

ALLOW_LABEL = "✅ 允许"
DENY_LABEL = "❌ 拒绝"

CallApi = Callable[..., dict[str, Any]]

# callback_data 有 64 字节硬上限，选项文字一律不进去——只带「哪张单的第几个」。
# nonce 8 位 hex + 两位序号，顶格 15 字节，离 64 远得很。
_CALLBACK_RE = re.compile(r"ask:([0-9a-f]+):(\d+)")

# getUpdates 游标。进程内簿记就够：同步模型一次只有一道题在等，
# 跨进程/跨会话的旧点击由发题前的 _drain 清掉。
_OFFSET: dict[str, int] = {}


# ---------- 宽度与布局 ----------
def width(text: str) -> float:
    """显示宽度，中文计。CJK/全角算 1 字，其余算半字——
    「16 字」这条线是拿她的手机实测出来的，不是拍脑袋。"""
    return sum(
        1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5
        for ch in text
    )


def pick_layout(labels: list[str], requested: str = "") -> str:
    """buttons＝文字直接上按钮；numbered＝正文列全文+编号按钮。
    auto 的判据只有一条：**有没有任何一个选项超过 16 字线**。"""
    mode = (requested or "auto").strip().lower()
    if mode not in ("auto", "buttons", "numbered"):
        raise ValueError('layout 只认 "auto" / "buttons" / "numbered"')
    if mode != "auto":
        return mode
    if all(width(label) <= BUTTON_WIDTH_LIMIT for label in labels):
        return "buttons"
    return "numbered"


def columns_for(labels: list[str], explicit: Any = None) -> int:
    """每行摆几个按钮：显式给了听你的；不给按选项长短自适应——
    A~E 一排五个最顺手，中等长度两个并排，再长一行一个（并排会被剪）。"""
    if explicit:
        try:
            n = int(str(explicit).strip())
        except ValueError:
            raise ValueError("columns 必须是整数") from None
        if n > 0:
            return min(n, MAX_COLUMNS)
    if all(width(label) <= 3 for label in labels):
        return min(5, len(labels))
    if all(width(label) <= 8 for label in labels):
        return 2
    return 1


_NUM_EMOJI = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


def numbered_label(idx: int) -> str:
    """0 起的序号 → 按钮脸：前十个用数字 emoji，之后退回普通数字。"""
    return _NUM_EMOJI[idx] if idx < len(_NUM_EMOJI) else str(idx + 1)


def build_keyboard(
    button_labels: list[str], nonce: str, per_row: int
) -> list[list[dict[str, str]]]:
    rows = []
    for start in range(0, len(button_labels), per_row):
        rows.append([
            {"text": label, "callback_data": f"ask:{nonce}:{idx}"}
            for idx, label in enumerate(
                button_labels[start:start + per_row], start=start
            )
        ])
    return rows


# ---------- 参数校验 ----------
def _labels(args: dict[str, Any]) -> list[str]:
    raw = args.get("options")
    if not isinstance(raw, list) or not raw:
        raise ValueError("options 至少给一个（字符串数组）")
    labels = [s for s in (str(x).strip() for x in raw) if s]
    if not labels:
        raise ValueError("options 里全是空字符串")
    if len(labels) > MAX_OPTIONS:
        raise ValueError(f"options 最多 {MAX_OPTIONS} 个，给了 {len(labels)} 个")
    for label in labels:
        if len(label) > LABEL_HARD_LIMIT:
            raise ValueError(
                f"选项「{label[:12]}…」长 {len(label)} 字符——"
                "这已经不是选项是文章了，精简到一句话"
            )
    return labels


def _timeout(args: dict[str, Any]) -> int:
    raw = args.get("timeout_s")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TIMEOUT
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError("timeout_s 必须是整数（秒）") from None
    if value < 0 or value > MAX_TIMEOUT:
        raise ValueError(f"timeout_s 要在 0~{MAX_TIMEOUT} 之间")
    return value


# ---------- getUpdates 轮询 ----------
def _poll(call_api: CallApi, data: dict[str, Any]) -> dict[str, Any]:
    try:
        return call_api("getUpdates", data)
    except ApiRejected as exc:
        if exc.code == 409:
            raise RuntimeError(
                "getUpdates 被别的消费者占着（HTTP 409）——按钮问答需要**专用 bot**。"
                "官方 telegram 插件、webhook 或另一个进程正在收这只 bot 的更新；"
                "去 @BotFather 给本 server 单独造一只，"
                "见 README「按钮问答要专用 bot」。"
            ) from None
        raise


def _drain(call_api: CallApi) -> None:
    """发题前把积压清到最新：上一题超时后的迟到点击、别处攒的旧 update，
    都不许混进这一轮等待。offset=-1 只回最新一条，顺便确认掉之前的全部。"""
    payload = _poll(call_api, {
        "offset": -1, "timeout": 0,
        "allowed_updates": json.dumps(["callback_query"]),
    })
    result = payload.get("result") or []
    if result:
        _OFFSET["v"] = int(result[-1]["update_id"]) + 1


def _ack(call_api: CallApi, callback_id: str, text: str = "") -> None:
    """answerCallbackQuery：不答对方客户端会转圈十几秒。
    ack 失败不影响答案本身——转圈由它去，别把已拿到的选择弄丢。"""
    data: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    try:
        call_api("answerCallbackQuery", data)
    except (RuntimeError, OSError):
        pass


def _wait_click(
    call_api: CallApi, nonce: str, chat: str, count: int, deadline: float
) -> int | None:
    """等属于这张单的第一下有效点击，返回选项序号；超时返回 None。

    私聊（chat_id 是正数）只认聊天对面那个人——别人拿到消息转发链接也点不动；
    群聊谁点都算（群本身就是「在场的人都有发言权」的场合）。
    """
    owner = chat if chat.isdigit() else ""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        data: dict[str, Any] = {
            "timeout": min(POLL_SLICE, max(1, int(remaining))),
            "allowed_updates": json.dumps(["callback_query"]),
        }
        if "v" in _OFFSET:
            data["offset"] = _OFFSET["v"]
        payload = _poll(call_api, data)
        for update in payload.get("result") or []:
            _OFFSET["v"] = int(update["update_id"]) + 1
            query = update.get("callback_query") or {}
            callback_id = str(query.get("id") or "")
            if not callback_id:
                continue
            match = _CALLBACK_RE.fullmatch(str(query.get("data") or ""))
            if not match or match.group(1) != nonce:
                _ack(call_api, callback_id, "这道题已经过期了")
                continue
            index = int(match.group(2))
            if index >= count:
                _ack(call_api, callback_id, "这道题已经过期了")
                continue
            clicker = str((query.get("from") or {}).get("id") or "")
            if owner and clicker != owner:
                _ack(call_api, callback_id, "Not authorized")
                continue
            _ack(call_api, callback_id)
            return index


# ---------- 发卡与收卡 ----------
def _send_card(
    call_api: CallApi, chat: str, text: str,
    keyboard: list[list[dict[str, str]]],
) -> int:
    payload = call_api("sendMessage", {
        "chat_id": chat,
        "text": text,
        "reply_markup": json.dumps(
            {"inline_keyboard": keyboard}, ensure_ascii=False
        ),
    })
    result = payload.get("result") or {}
    return int(result.get("message_id") or 0)


def _settle_card(
    call_api: CallApi, chat: str, message_id: int, text: str
) -> None:
    """收按钮：原地改正文、去掉键盘（editMessageText 不带 reply_markup＝键盘没了）。
    没人再轮询的按钮是幽灵按钮——点了永远转圈，必须收。
    收卡失败不致命：答案已经到手，别为一次 edit 把整轮问答报废。"""
    try:
        call_api("editMessageText", {
            "chat_id": chat, "message_id": message_id, "text": text[:TEXT_LIMIT],
        })
    except (RuntimeError, OSError):
        pass


# ---------- 工具：选择题 ----------
def tool_ask_choice(args: dict[str, Any], chat: str, call_api: CallApi) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("question 不能为空")
    labels = _labels(args)
    layout = pick_layout(labels, str(args.get("layout") or ""))
    timeout_s = _timeout(args)
    mark = args.get("mark_answered")
    mark = True if mark is None else bool(mark)
    chat = str(chat)

    nonce = secrets.token_hex(4)
    if layout == "buttons":
        body = question
        per_row = columns_for(labels, args.get("columns"))
        keyboard = build_keyboard(labels, nonce, per_row)
    else:
        listing = "\n".join(
            f"{numbered_label(i)} {label}" for i, label in enumerate(labels)
        )
        body = f"{question}\n\n{listing}"
        keyboard = build_keyboard(
            [numbered_label(i) for i in range(len(labels))],
            nonce, min(5, len(labels)),
        )
    if len(body) > TEXT_LIMIT:
        raise ValueError(
            f"题干+选项列表共 {len(body)} 字，超过 Telegram 的 {TEXT_LIMIT} 上限——"
            "精简题干，或把长选项挪进题干里概括"
        )

    _drain(call_api)
    message_id = _send_card(call_api, chat, body, keyboard)
    index = _wait_click(
        call_api, nonce, chat, len(labels), time.time() + timeout_s
    )
    if index is None:
        if mark:
            _settle_card(call_api, chat, message_id,
                         f"{body}\n\n⌛ 超时未答（按钮已失效）")
        raise RuntimeError(
            f"等了 {timeout_s}s 没人点按钮（message_id: {message_id}）。"
            "要重问就再调一次。"
        )
    option = labels[index]
    if mark:
        chosen = option if layout == "buttons" else f"{numbered_label(index)} {option}"
        _settle_card(call_api, chat, message_id, f"{body}\n\n✅ 已选：{chosen}")
    return json.dumps(
        {"index": index, "option": option, "message_id": message_id},
        ensure_ascii=False,
    )


# ---------- 工具：权限审批 ----------
def _redact_line(line: str) -> str:
    """一行一判：命中密钥关键词或密钥形态就整行隐去。
    逐行而不是整块——审批的人得看见参数才叫审批，
    但看不见的那一行宁可少看，不能是密钥。闸的真源在
    tg_progress_hook（DIRTY/SECRET_SHAPES），这里只是复用，绝不另抄。"""
    lowered = line.lower()
    if any(word in lowered for word in DIRTY):
        return "…（该行含敏感关键词，已隐去）"
    if any(shape.search(line) for shape in SECRET_SHAPES):
        return "…（该行含密钥形态，已隐去）"
    return line


def summarize_input(tool_input: Any, limit: int = 900) -> str:
    """把工具参数摆给审批人看：pretty JSON、逐行过闸、超长截断。"""
    try:
        pretty = json.dumps(tool_input, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        pretty = str(tool_input)
    pretty = redact_telegram_tokens(pretty)
    text = "\n".join(_redact_line(line) for line in pretty.splitlines())
    if len(text) > limit:
        text = text[:limit] + "\n…（截断）"
    return text


def tool_ask_permission(args: dict[str, Any], chat: str, call_api: CallApi) -> str:
    """Claude Code `--permission-prompt-tool` 的契约实现。

    返回值**只能**是契约 JSON 本身（allow 带 updatedInput / deny 带 message），
    多一个字 Claude Code 就解析不了。三个出口全走 fail-closed：
    点拒绝＝拒绝，超时＝拒绝，只有明确点了允许才放行。
    """
    tool_name = str(args.get("tool_name") or "").strip()
    if not tool_name:
        raise ValueError("tool_name 不能为空（Claude Code 会自动带上）")
    chat = str(chat)
    if not chat.isdigit():
        raise ValueError(
            "权限审批只走私聊（chat_id 得是正的用户 id）——"
            "群里谁都能点「允许」，那不是审批是抽奖"
        )
    tool_input = args.get("input")
    if isinstance(tool_input, str):
        # host 有时把 input 序列化成字符串递进来，能解回对象就解
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            pass
    timeout_s = _timeout(args)

    body = (
        f"⚙️ 权限请求\n"
        f"Claude 想调用：{tool_name}\n\n"
        f"{summarize_input(tool_input)}\n\n"
        f"⏳ {timeout_s}s 内没回应＝自动拒绝"
    )
    nonce = secrets.token_hex(4)
    keyboard = build_keyboard([ALLOW_LABEL, DENY_LABEL], nonce, 2)

    _drain(call_api)
    message_id = _send_card(call_api, chat, body[:TEXT_LIMIT], keyboard)
    index = _wait_click(call_api, nonce, chat, 2, time.time() + timeout_s)
    if index == 0:
        _settle_card(call_api, chat, message_id, f"{body}\n\n✅ 已允许")
        contract: dict[str, Any] = {
            "behavior": "allow",
            "updatedInput": tool_input if isinstance(tool_input, dict) else {},
        }
    elif index == 1:
        _settle_card(call_api, chat, message_id, f"{body}\n\n❌ 已拒绝")
        contract = {"behavior": "deny", "message": "用户在 Telegram 上点了拒绝"}
    else:
        _settle_card(call_api, chat, message_id, f"{body}\n\n⌛ 超时，视为拒绝")
        contract = {
            "behavior": "deny",
            "message": f"{timeout_s}s 内无人审批，按拒绝处理（fail-closed）",
        }
    return json.dumps(contract, ensure_ascii=False)
