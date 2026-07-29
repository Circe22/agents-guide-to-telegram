#!/usr/bin/env python3
"""tg-rich-mcp —— 把 Telegram Bot API 的 Rich Message 接进握手式 MCP host。

（握手式＝走 initialize/initialized 那几版协议，2024-11-05 ~ 2025-11-25；
2026-07-28 起的无状态新协议是另一套，见 README「支持哪几版 MCP 协议」。）

官方 telegram 插件的 reply 只能发 text / files / 引用，够不着 Rich Message。
这个 server 直投 Bot API，让 agent 能发：原生表格、LaTeX 公式、折叠块、
勾选清单、引用、分割线，以及**流式草稿**（会自己变的消息）。

三个工具：
  tg_rich_send    发一条正式富消息（进聊天记录，永久保留），返回 message_id
  tg_rich_edit    原地改一条已发出的富消息——**持久进度窗**靠它，
                  不受草稿 30 秒限制、留在聊天记录里、编辑不响铃
  tg_rich_draft   推一帧流式草稿（30 秒临时预览，私聊限定，不进聊天记录）
                  ⚠️ 草稿活跃期间会锁死 Telegram Android 的发送框，见 README「坑」③

配置，两个来源都行（环境变量优先）：
  ① 环境变量：TG_BOT_TOKEN（必填）/ TG_CHAT_ID（可选）/ TG_PROXY（可选）
  ② 配置文件 ~/.tg-rich-mcp.json：{"bot_token": "...", "chat_id": "...", "proxy": "..."}

⚠️ 想同时用配套的进度窗 hook，**就得用配置文件**——hook 是编辑器另起的进程，
   拿不到你写在 MCP server 那段 env 里的变量。这是最容易卡住的一步。
   记得 chmod 600 ~/.tg-rich-mcp.json

依赖：只有 requests。协议是手写的 JSON-RPC over stdio，不需要 mcp SDK。

⚠️ 这条管道是 JSON-RPC，**绝不能往 stdout 打字**——多一行就把协议冲了。
   要调试请写 stderr。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

from secret_redaction import redact_telegram_tokens

SERVER_NAME = "tg-rich"
SERVER_VERSION = "1.0.0"
# 本 server 实现的是**握手式**（initialize/initialized）的 MCP，
# 覆盖 2024-11-05 ~ 2025-11-25 这几版。
#
# ⚠️ **2026-07-28 那版不在此列**，而且不是"再加一个字符串"就能支持的：
# 它把 MCP 改成了无状态协议——移除 initialize/notifications/initialized 握手，
# 协议版本与客户端能力改为每个请求放在 `_meta` 里带；服务器 MUST 实现
# `server/discover`；所有 result 必须带 `resultType`；`ping` / `logging/setLevel` 移除。
# 见 <https://modelcontextprotocol.io/specification/2026-07-28> 的 Key Changes。
# 好消息是它给了向后兼容的路：新客户端可以拿 `server/discover` 当探测，
# 我们回 method not found，它就知道该按旧协议来。
PROTOCOL_VERSION = "2025-11-25"
# 版本协商：客户端要的版本我支持，就回同一个；不支持才回自己最新的、让它决定断不断
# （规范要求双方在 initialize 时协商，写死一个版本会让严格的 host 直接断开）。
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TG_API = "https://api.telegram.org"

# 官方原文：Exactly one of the fields html, markdown, or blocks must be used.
CONTENT_FIELDS = ("markdown", "html", "blocks")


# ---------- 配置 ----------
CONFIG_PATH = Path.home() / ".tg-rich-mcp.json"


def _file_config(_cache: dict[str, Any] = {}) -> dict[str, Any]:
    """读 ~/.tg-rich-mcp.json。读不到就当空——缺配置不该让进程起不来。"""
    if "v" not in _cache:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _cache["v"] = data if isinstance(data, dict) else {}
        except Exception:
            _cache["v"] = {}
    return _cache["v"]


def _conf(env_key: str, file_key: str) -> str:
    """环境变量优先，其次配置文件。"""
    value = (os.environ.get(env_key) or "").strip()
    if value:
        return value
    return str(_file_config().get(file_key) or "").strip()


def _token() -> str:
    return _conf("TG_BOT_TOKEN", "bot_token")


def _default_chat() -> str:
    return _conf("TG_CHAT_ID", "chat_id")


def _proxies() -> dict[str, str] | None:
    proxy = _conf("TG_PROXY", "proxy")
    return {"https": proxy, "http": proxy} if proxy else None


def _scrub(message: str, token: str) -> str:
    """token 绝不出现在返回里。

    requests 的连接异常经常把整条 URL 塞进消息，而 URL 里就有 token——
    这是最容易漏的一处，别省。
    """
    return message.replace(token, "<token>") if token else message


def _scrub_out(message: str) -> str:
    """出口脱敏：既抹自家 token，也抹掉调用方误塞进参数里的任何 bot token 形态。"""
    try:
        message = _scrub(message, _token())
    except Exception:
        pass
    # 形态闸抹的是**别人的** token（调用方误塞进参数里的），精确替换够不着。
    # 正则本身在 secret_redaction.py，单一真源——抄两份必然漂移，已经漂过一次。
    return redact_telegram_tokens(message)


# ---------- JSON-RPC 小工具 ----------
def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    # 出口统一脱敏。以前只有 requests 那条路过 _scrub，可校验类异常（int() 报错、
    # unknown tool 回显入参）会把原值原样带出去——而调用方可能刚好把 token 填错了位置。
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": _scrub_out(message)}}


def _text(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}]}


def _text_error(body: str) -> dict[str, Any]:
    """工具**执行**失败的正确返回姿势（MCP 规范）。

    Telegram 拒收、没配 token、参数写错——这些是"工具跑了但没成"，属于**执行错误**，
    要放进正常结果里标 `isError: true`，模型才读得到原因、自己改了重试。
    JSON-RPC error 留给**协议层**问题（未知方法、params 不是对象、工具名不存在）——
    那种错误模型改不了，抛给 host 才对。

    出口一样过脱敏：**每条出口都要过**，不是只包网络那一段。
    """
    return {"content": [{"type": "text", "text": _scrub_out(body)}], "isError": True}


# ---------- 核心 ----------
def build_rich(args: dict[str, Any]) -> dict[str, Any]:
    """把工具参数拼成 InputRichMessage。三选一的约束在这里守。"""
    markdown = str(args.get("markdown") or "").strip()
    html = str(args.get("html") or "").strip()
    blocks = args.get("blocks")

    given = [bool(markdown), bool(html), blocks is not None]
    if sum(given) != 1:
        raise ValueError(
            "markdown / html / blocks 三选一，必须且只能给一个"
            f"（这次给了 {sum(given)} 个）"
        )

    rich: dict[str, Any] = {}
    if blocks is not None:
        # 有些 host 会把数组序列化成字符串塞进来，容一下
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except json.JSONDecodeError as exc:
                raise ValueError(f"blocks 不是合法 JSON：{exc}") from None
        if not isinstance(blocks, list):
            raise ValueError("blocks 必须是数组")
        rich["blocks"] = blocks
    elif markdown:
        rich["markdown"] = markdown
    else:
        rich["html"] = html

    if args.get("rtl"):
        rich["is_rtl"] = True
    return rich


def call_api(method: str, data: dict[str, Any]) -> dict[str, Any]:
    token = _token()
    if not token:
        raise RuntimeError(
            "没找到 bot token：设环境变量 TG_BOT_TOKEN，"
            f"或在 {CONFIG_PATH} 里写 {{\"bot_token\": \"…\"}}"
        )
    try:
        response = requests.post(
            f"{TG_API}/bot{token}/{method}",
            data=data,
            proxies=_proxies(),
            timeout=45,
        )
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"发送失败: {type(exc).__name__}: {_scrub(str(exc), token)}"
        ) from None
    if not payload.get("ok"):
        raise RuntimeError(f"API 拒收: {_scrub(str(payload.get('description')), token)}")
    return payload


def _resolve_chat(args: dict[str, Any]) -> str:
    chat = str(args.get("chat_id") or "").strip() or _default_chat()
    if not chat:
        raise RuntimeError(
            f"没给 chat_id，也没配默认值（TG_CHAT_ID 或 {CONFIG_PATH} 的 chat_id）"
        )
    return chat


def _int_arg(args: dict[str, Any], key: str, what: str) -> int:
    """整数参数统一在这儿转，报错说人话。

    别让 Python 的 `invalid literal for int()...` 直接出门——那条消息会把**原值**
    带出去，而调用方可能刚好把 token 填错了位置。
    """
    raw = str(args.get(key) or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} 必须是整数（{what}）") from None


def tool_send(args: dict[str, Any]) -> str:
    rich = build_rich(args)
    data: dict[str, Any] = {
        "chat_id": _resolve_chat(args),
        "rich_message": json.dumps(rich, ensure_ascii=False),
    }
    if args.get("silent"):
        data["disable_notification"] = "true"
    reply_to = _int_arg(args, "reply_to", "要引用的那条消息的 message_id")
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to})

    payload = call_api("sendRichMessage", data)
    result = payload.get("result")
    mid = result.get("message_id") if isinstance(result, dict) else result
    return (
        f"富消息已送达（message_id: {mid}）。"
        f"想做持久进度窗就记住这个 id，之后用 tg_rich_edit 原地改它。"
    )


def tool_edit(args: dict[str, Any]) -> str:
    """原地改一条已发出的富消息。

    这是**持久进度窗**的做法：开工先 tg_rich_send 发一条，记住 message_id，
    之后每帧 edit 它——不受草稿 30 秒的限制、留在聊天记录里、编辑不响铃。
    """
    message_id = _int_arg(args, "message_id", "tg_rich_send 返回的那个 id")
    if not message_id:
        raise ValueError("要改哪条？给 message_id（tg_rich_send 的返回里有）")
    rich = build_rich(args)
    call_api("editMessageText", {
        "chat_id": _resolve_chat(args),
        "message_id": message_id,
        "rich_message": json.dumps(rich, ensure_ascii=False),
    })
    return f"消息 {message_id} 已就地更新（不响铃）"


def tool_draft(args: dict[str, Any]) -> str:
    draft_id = _int_arg(args, "draft_id", "任意非零整数，同一个 id 才会做动画过渡")
    if not draft_id:
        raise ValueError("draft_id 必须是非零整数（同一个 id 的连续调用才会做动画过渡）")
    chat = _resolve_chat(args)
    if chat.startswith("-"):
        # 群/频道 id 以 - 开头。与其等 API 拒收，不如当场说清楚
        raise ValueError("草稿只能发私聊；群里要实时进度请用 tg_rich_send + tg_rich_edit")
    rich = build_rich(args)
    call_api("sendRichMessageDraft", {
        "chat_id": chat,
        "draft_id": draft_id,
        "rich_message": json.dumps(rich, ensure_ascii=False),
    })
    return (
        f"草稿已推（draft_id={draft_id}）。它只活 30 秒、不进聊天记录——"
        "内容定稿后必须再调一次 tg_rich_send 才留得住。"
        "长任务想要不消失的进度窗，改用 tg_rich_send + tg_rich_edit。"
    )


# ---------- 工具 schema ----------
# 配方。这几行是刻意写的：blocks 是原样透传的，能力早就在那儿，
# **但工具描述里没写的东西，agent 不会去试**——对它来说等于不存在。
# 所以这里不列字段名，直接给能套用的形状。
def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "tg_rich_send":
        return _text(tool_send(args))
    if name == "tg_rich_edit":
        return _text(tool_edit(args))
    if name == "tg_rich_draft":
        return _text(tool_draft(args))
    raise ValueError("unknown tool")
_RECIPES = (
    "\n\n【配方 · 直接套】\n"
    "① 句子里嵌公式（不用整块打断）：\n"
    '   {"type":"paragraph","text":["当 ",'
    '{"type":"mathematical_expression","expression":"x^2-5x+6=0"}," 时…"]}\n'
    "② 答案遮住，点开才见（题卡、剧透）：\n"
    '   {"type":"paragraph","text":["答案：",{"type":"spoiler","text":"B"}]}\n'
    "③ 上下标：\n"
    '   {"type":"paragraph","text":["H",{"type":"subscript","text":"2"},"O"]}\n'
    "④ 折叠长内容（收起只占一行）：\n"
    '   {"type":"details","summary":"展开看细节","blocks":[…]}\n'
    "⑤ 长报告目录跳转：\n"
    '   {"type":"anchor","name":"s1"} + '
    '{"type":"paragraph","text":[{"type":"anchor_link","text":"跳到第一节","anchor_name":"s1"}]}\n'
    "⑥ 带勾选框的清单：\n"
    '   {"type":"list","items":[{"has_checkbox":true,"is_checked":true,'
    '"blocks":[{"type":"paragraph","text":"做完了"}]}]}\n'
    "⑦ 持久进度窗（三步）：tg_rich_send 发一条 → 记住返回的 message_id → "
    "每帧 tg_rich_edit 改它。"
)


_CONTENT_SCHEMA = {
    "markdown": {
        "type": "string",
        "description": "Markdown 正文。日常首选，一行字就能发。",
    },
    "html": {
        "type": "string",
        "description": "HTML 正文。支持 <table> <details> <tg-math-block> 等标签。",
    },
    "blocks": {
        "type": "array",
        "items": {"type": "object"},
        "description": (
            "块数组，最可控。**块级** type：paragraph / heading(size 1-6) / pre(language) / "
            "footer / divider / mathematical_expression(字段名是 expression，裸 LaTeX 不要包 $$) / "
            "list(items[]，支持 has_checkbox·is_checked) / blockquote(blocks[]·credit) / "
            "pullquote(text·credit) / table(cells[][]·is_bordered·is_striped·caption) / "
            "details(summary·blocks[]·is_open) / anchor(name) / map(location·zoom·width·height) / "
            "collage·slideshow(blocks[]·caption) / photo·video·audio·animation·voice_note / "
            "thinking(仅 draft 可用)。"
            "表格单元格：text·is_header·colspan·rowspan·align·valign。"
            "**行内样式**：任何 text 字段都可以传数组，元素是字符串或 "
            "{type, text}——type 可为 bold / italic / underline / strikethrough / code / "
            "spoiler(点开才可见) / marked(高亮) / subscript / superscript / "
            "mathematical_expression(字段 expression，行内公式) / url(字段 url) / "
            "reference(脚注，配 anchor 块) / anchor_link(字段 anchor_name，页内跳转) / "
            "date_time(字段 unix_time，按读者时区渲染) / custom_emoji(custom_emoji_id + alternative_text)。"
            "表格单元格的 text 同样收数组。"
        ) + _RECIPES,
    },
    "chat_id": {
        "type": "string",
        "description": "目标聊天；不给就用环境变量 TG_CHAT_ID。",
    },
    "rtl": {"type": "boolean", "description": "右到左排版。"},
}

TOOLS = (
    {
        "name": "tg_rich_send",
        "description": (
            "给 Telegram 发一条**富消息**：原生表格、LaTeX 公式、折叠块、勾选清单、"
            "引用、分割线。普通 sendMessage 发不了这些。"
            "markdown / html / blocks 三选一——日常用 markdown 最省事，"
            "表格和公式这类结构化的东西用 blocks。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_CONTENT_SCHEMA,
                "silent": {"type": "boolean", "description": "静默发送不响铃。"},
                "reply_to": {"type": "string", "description": "引用某条 message_id。"},
            },
        },
    },
    {
        "name": "tg_rich_edit",
        "description": (
            "原地改一条已经发出去的富消息（`editMessageText` 收 `rich_message`）。"
            "**这是长任务进度窗的正确做法**：开工先 tg_rich_send 发一条、记住返回的 message_id，"
            "之后每帧调本工具改它——不受草稿 30 秒限制、留在聊天记录里、编辑不响铃。"
            "内容同样是 markdown / html / blocks 三选一。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_CONTENT_SCHEMA,
                "message_id": {
                    "type": "integer",
                    "description": "要改哪条——tg_rich_send 的返回里给了这个 id。",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "tg_rich_draft",
        "description": (
            "推一帧**流式草稿**——会自己变的消息，适合长任务的进度窗。"
            "三条硬约束：① 只能发私聊；② 草稿只活 30 秒且不进聊天记录；"
            "③ 内容定稿后必须再调一次 tg_rich_send 才留得住。"
            "同一个 draft_id 的连续调用会在客户端做动画过渡。"
            "⚠️ 它不是聊天手段——想让对方留着的话，用 tg_rich_send。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_CONTENT_SCHEMA,
                "draft_id": {
                    "type": "integer",
                    "description": "非零整数。同一个 id 连续调用＝同一个窗口在变。",
                },
            },
            "required": ["draft_id"],
        },
    },
)

KNOWN_TOOLS = frozenset(tool["name"] for tool in TOOLS)

INSTRUCTIONS = (
    "要发表格、LaTeX 公式、折叠块、勾选清单或长报告时，用 tg_rich_send"
    "（普通的 telegram 发送工具做不到这些）；纯文字聊天照旧走原来的发送工具。\n"
    "长任务的进度窗用 tg_rich_send 发一条 + tg_rich_edit 反复原地改（不响铃、留记录）；"
    "tg_rich_draft 只在要那种 30 秒动画质感时才用——它会消失，不是聊天手段。\n"
    "⚠️ 最容易被忽略的一点：**任何 text 字段都能传数组**，"
    "于是公式、遮挡、上下标、高亮可以嵌在句子中间，而不必单独占一块。"
    "写内容前先看一眼 blocks 参数描述末尾的配方。"
)


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")

    # 没有 id ＝ notification，不期待任何响应。这个判断必须在分发**之前**：
    # 放在最后的话，`{"method":"tools/list"}` 会先被执行、再回一条 id=null 的响应；
    # 更糟的是有人把 tools/call 当 notification 发进来——消息真发出去了，
    # 调用方却拿不到任何结果。（本 server 不消费任何客户端通知。）
    if "id" not in message:
        return None
    request_id = message["id"]

    # 别写 `or {}`：那样 [] / "" / 0 会被静默当成"没给 params"，
    # 后面的类型检查根本看不到原始的错误类型。缺省只有 None 一种。
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # 合法 JSON 但 params 是数组时，下面的 .get() 会 AttributeError 掀掉整个 server
        return _error(request_id, -32602, "params must be an object")
    if method == "initialize":
        wanted = str(params.get("protocolVersion") or "")
        return _result(
            request_id,
            {
                "protocolVersion": wanted if wanted in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": list(TOOLS)})
    if method == "tools/call":
        name = str(params.get("name") or "")
        # 别写 `or {}`：那样 [] / "" / 0 会被静默当成"没给参数"放过去，
        # 只有非空的错误类型才拦得住。缺省只有 None 一种。
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments must be an object")
        if name not in KNOWN_TOOLS:
            # 工具名不存在＝协议层问题，模型改不了，抛给 host
            return _error(request_id, -32602, f"unknown tool: {name}")
        try:
            return _result(request_id, _call(name, arguments))
        except (RuntimeError, ValueError, OSError) as exc:
            # 跑了但没成（API 拒收 / 缺 token / 参数写错）＝执行错误，
            # 放进正常结果标 isError，模型读得到原因、能自己改了重试
            return _result(request_id, _text_error(str(exc)))
    return _error(request_id, -32601, "method not found")   # notification 已在开头挡掉


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            response = handle(message)
        except (json.JSONDecodeError, ValueError) as exc:
            response = _error(None, -32700, str(exc))
        except Exception as exc:
            # 单条消息的最终兜底：任何没预料到的异常都不许掀掉读循环，
            # 也不许把 traceback 打上 stderr——server 一崩，整个 MCP 就掉线了。
            response = _error(
                message.get("id") if isinstance(message, dict) else None,
                -32603,
                f"internal error: {type(exc).__name__}",
            )
        if response is not None:
            sys.stdout.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
