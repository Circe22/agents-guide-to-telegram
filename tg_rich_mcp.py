#!/usr/bin/env python3
"""tg-rich-mcp —— 把 Telegram Bot API 的 Rich Message 接进握手式 MCP host。

（握手式＝走 initialize/initialized 那几版协议，2024-11-05 ~ 2025-11-25；
2026-07-28 起的无状态新协议是另一套，见 README「支持哪几版 MCP 协议」。）

官方 telegram 插件的 reply 只能发 text / files / 引用，够不着 Rich Message。
这个 server 直投 Bot API，让 agent 能发：原生表格、LaTeX 公式、折叠块、
勾选清单、引用、分割线、多图拼贴/轮播/地图（本地文件经 media_paths 上传，
或用 file_id / 外链复用），以及**流式草稿**（会自己变的消息）。

六个工具：
  tg_rich_send        发一条正式富消息（进聊天记录，永久保留），返回 message_id；
                      markdown 正文里的（emoji）标记会剥成真贴纸（库非空时）
  tg_rich_edit        原地改一条已发出的富消息——**持久进度窗**靠它，
                      不受草稿 30 秒限制、留在聊天记录里、编辑不响铃
  tg_rich_draft       推一帧流式草稿（30 秒临时预览，私聊限定，不进聊天记录）
                      ⚠️ 草稿活跃期间会锁死 Telegram Android 的发送框，见 README「坑」③
  tg_sticker_send     从贴纸库挑一张真贴纸直发（emoji 交集 / id / query；无参＝馆藏清单）
  tg_sticker_import   收到的贴纸下载归档→看图起标题配标签→认领入库（贴纸车道见 COOKBOOK）
  tg_ask_choice       发带按钮的选择题并**同步等答案**——对方点哪个，工具就返回哪个
                      ⚠️ 实验性；需要专用 bot（getUpdates 独占），见 README「按钮问答要专用 bot」

（手机权限审批卡 tg_ask_permission 设计已定稿、参考实现进过一版又主动下架——
没实弹验证过稳定性的东西不上架，详见 README「还没做的」。）

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

import tg_ask
import tg_sticker
from secret_redaction import redact_telegram_tokens
from tg_sticker import ApiRejected

SERVER_NAME = "tg-rich"
SERVER_VERSION = "1.3.1"
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


# ---------- 媒体上传 ----------
MEDIA_MAX_BYTES = 50 * 1024 * 1024   # Bot API 上限：上传文件最大 50MB
MEDIA_MAX_COUNT = 50                  # 官方：一条富消息最多 50 个媒体附件

# 文件名形态闸：agent 拿到的是"发这个路径"，它自己不看内容——
# 这道闸拦的是把凭证文件当图发出去的那类事故。保守设计，会误伤
# "my_secret_santa.jpg" 这种名字，所以给了开关；报错文案会说清怎么关。
_SENSITIVE_NAME_RE = re.compile(
    r"^\.env($|\.)|^id_(rsa|ed25519|ecdsa|dsa)($|\.)|\.(pem|key|p12|pfx|ppk)$"
    r"|token|credential|secret|api_?key|passwd|password|^\.netrc$|\.git-credentials$",
    re.IGNORECASE,
)


def _media_guard_on() -> bool:
    return os.environ.get("TG_RICH_MEDIA_GUARD", "1").strip() != "0"


def load_media(paths: Any) -> dict[str, tuple[str, bytes]]:
    """把 media_paths 读成 multipart 字典：第 i 个路径 → 附件名 f{i}。

    blocks 里用 attach://f0 引用第 0 个文件，以此类推。
    符号链接按**真实目标**检查——链接名无害不代表指向的东西无害。
    """
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("media_paths 必须是字符串数组（本地文件的绝对路径）")
    if len(paths) > MEDIA_MAX_COUNT:
        raise ValueError(f"一条富消息最多 {MEDIA_MAX_COUNT} 个媒体（给了 {len(paths)} 个）")

    files: dict[str, tuple[str, bytes]] = {}
    for i, raw in enumerate(paths):
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ValueError(f"media_paths[{i}] 不是文件：{raw}")
        real = path.resolve()
        if _media_guard_on() and (
            _SENSITIVE_NAME_RE.search(path.name) or _SENSITIVE_NAME_RE.search(real.name)
        ):
            raise ValueError(
                f"media_paths[{i}] 的文件名看着像凭证（{path.name}），不发。"
                "确认无害的话设 TG_RICH_MEDIA_GUARD=0 再试"
            )
        size = real.stat().st_size
        if size > MEDIA_MAX_BYTES:
            raise ValueError(
                f"media_paths[{i}] 超过 Bot API 的 50MB 上限"
                f"（{size / 1024 / 1024:.0f}MB）：{path.name}"
            )
        files[f"f{i}"] = (path.name, real.read_bytes())
    return files


def extract_file_ids(result: Any) -> list[str]:
    """从 sendRichMessage 的响应里挖上传媒体的 file_id，按出现顺序。

    photo 是尺寸变体数组（取最大那档）；video/audio 等是单对象。
    存下 file_id 之后复用不用重新上传——photo 块的 media 直接填它。
    ⚠️ 复用时**整串程序化取用**，别看着截断的显示手补尾巴。
    """
    ids: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            photo = node.get("photo")
            if isinstance(photo, list) and photo and all(
                isinstance(v, dict) and "file_id" in v for v in photo
            ):
                best = max(photo, key=lambda v: v.get("width", 0) * v.get("height", 0))
                ids.append(str(best["file_id"]))
            for key, value in node.items():
                if key == "photo":
                    continue
                if isinstance(value, dict) and "file_id" in value:
                    ids.append(str(value["file_id"]))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)
    return ids


# ---------- 核心 ----------
# 本会话每个 chat 最后一条「可原地编辑」的消息（贴纸不算——贴纸没有 editMessageText）。
# 给 tg_rich_edit 的簿记减负：进度窗循环里 agent 不用自己记 message_id。
# 只活在进程内存里，重启即忘——比落盘更诚实：跨会话去改一条旧消息才是事故。
_LAST_SENT: dict[str, int] = {}


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


def call_api(
    method: str,
    data: dict[str, Any],
    files: dict[str, tuple[str, bytes]] | None = None,
) -> dict[str, Any]:
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
            files=files or None,
            proxies=_proxies(),
            # 定长 multipart 走代理实测能过，但大文件要给足时间
            timeout=180 if files else 45,
        )
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"发送失败: {type(exc).__name__}: {_scrub(str(exc), token)}"
        ) from None
    if not payload.get("ok"):
        # 类型化异常带 error_code：贴纸懒迁移只认 400 才算 file_id 失效，
        # 不许拿报错文案当接口去猜。ApiRejected 继承 RuntimeError，
        # 原有的 except 一个都不用改。
        code = payload.get("error_code")
        raise ApiRejected(
            f"API 拒收: {_scrub(str(payload.get('description')), token)}",
            int(code) if isinstance(code, int) else 0,
        )
    return payload


def download_file(remote_path: str) -> bytes:
    """按 getFile 给的 file_path 把文件拉下来（贴纸归档用）。"""
    token = _token()
    if not token:
        raise RuntimeError("没找到 bot token（TG_BOT_TOKEN 或配置文件 bot_token）")
    try:
        response = requests.get(
            f"{TG_API}/file/bot{token}/{remote_path}",
            proxies=_proxies(), timeout=120,
        )
    except Exception as exc:
        raise RuntimeError(
            f"下载失败: {type(exc).__name__}: {_scrub(str(exc), token)}"
        ) from None
    if response.status_code != 200:
        raise RuntimeError(f"下载失败: HTTP {response.status_code}")
    return response.content


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


def _send_with_stickers(parts: list[tuple[str, Any]], args: dict[str, Any]) -> str:
    """按标记位置分段发：文字段走富消息，标记处发真贴纸。

    坑 17 的纪律就落在这儿：**分段记账**。贴纸段失败不牵连已送达的文字
    （话已经送到了，脸没送到只记一笔），整条也绝不自动重试。

    孤儿贴纸防护：脸是贴给它前面那句话的（位置即语义），所以**脸不许先于
    它所依附的那句话出门**。标记写在句首时贴纸段排在文字段前面，若照顺序发，
    贴纸成功、正文随后失败＝对方收到一张没头没尾的脸——比缺一张脸更糟，那是
    把语气安在了一句不存在的话上。规则：

    - 整条没有正文段（纯贴纸）：照常直发，不存在孤儿问题；
    - 有正文段：贴纸挂起，第一条正文真送达后立刻补发，位置不变；
    - 正文抛错：整条中断，挂起的脸永不发送（宁可什么都没收到）；
    - 反过来不变：贴纸自己发失败，依旧不牵连正文（话比脸要紧）。
    """
    chat = _resolve_chat(args)
    token = _token()
    reply_to = _int_arg(args, "reply_to", "要引用的那条消息的 message_id")
    lines: list[str] = []
    has_text = any(kind == "text" and payload for kind, payload in parts)
    text_delivered = False
    held: list[Any] = []   # 正文落地前挂起的贴纸段

    def _fire(payload: Any) -> None:
        pool, combo, raw = payload
        try:
            entry = tg_sticker.pick(pool, combo)
            tg_sticker.send_entry(entry, chat, token, call_api)
            lines.append(f"贴纸「{entry.get('title')}」← 标记 {raw}")
        except (RuntimeError, ValueError, OSError) as exc:
            lines.append(f"贴纸未送达 ← 标记 {raw}（{exc}）")

    for kind, payload in parts:
        if kind == "text":
            if not payload:
                continue
            data: dict[str, Any] = {
                "chat_id": chat,
                "rich_message": json.dumps({"markdown": payload}, ensure_ascii=False),
            }
            if args.get("silent"):
                data["disable_notification"] = "true"
            if not text_delivered and reply_to:
                data["reply_parameters"] = json.dumps({"message_id": reply_to})
            response = call_api("sendRichMessage", data)
            result = response.get("result")
            mid = result.get("message_id") if isinstance(result, dict) else result
            if isinstance(mid, int):
                _LAST_SENT[chat] = mid
            lines.append(f"文字段（message_id: {mid}）")
            text_delivered = True
            for pending in held:
                _fire(pending)
            held.clear()
        else:
            if has_text and not text_delivered:
                held.append(payload)   # 脸先憋着，等它依附的话真送到
            else:
                _fire(payload)
    for pending in held:   # 走完了正文却一条都没送出去：记账，不补发
        lines.append(f"贴纸未送达 ← 标记 {pending[2]}（正文未送达，孤儿防护挂起）")
    return (
        "已按标记位置分段送达：\n  " + "\n  ".join(lines)
        + "\n（句内贴纸标记层可用 TG_STICKER_MARKERS=0 整体关闭）"
    )


def tool_send(args: dict[str, Any]) -> str:
    # 渲染器模式的第二层：markdown 正文里的（emoji）标记剥成真贴纸，
    # 写到哪儿贴纸跟在哪条后面（位置即语义）。库为空/标记没命中时零开销、零改动。
    markdown_raw = str(args.get("markdown") or "")
    if markdown_raw.strip() and not args.get("media_paths"):
        parts = tg_sticker.split_message(markdown_raw)
        if any(kind == "sticker" for kind, _ in parts):
            return _send_with_stickers(parts, args)

    rich = build_rich(args)
    media = None
    if args.get("media_paths"):
        if "blocks" not in rich:
            raise ValueError(
                "media_paths 目前只配 blocks 用：blocks 里放 photo 块、"
                'media 填 "attach://f0" 引用第 0 个文件'
                "（markdown/html 的媒体引用是另一套 tg://photo?id=，本工具暂未接）"
            )
        media = load_media(args["media_paths"])
    data: dict[str, Any] = {
        "chat_id": _resolve_chat(args),
        "rich_message": json.dumps(rich, ensure_ascii=False),
    }
    if args.get("silent"):
        data["disable_notification"] = "true"
    reply_to = _int_arg(args, "reply_to", "要引用的那条消息的 message_id")
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to})

    payload = call_api("sendRichMessage", data, files=media)
    result = payload.get("result")
    mid = result.get("message_id") if isinstance(result, dict) else result
    if isinstance(mid, int):
        _LAST_SENT[data["chat_id"]] = mid
    reply = (
        f"富消息已送达（message_id: {mid}）。"
        f"想做持久进度窗就记住这个 id，之后用 tg_rich_edit 原地改它。"
    )
    if media:
        ids = extract_file_ids(result)
        if ids:
            listing = "\n".join(f"  {n}. {fid}" for n, fid in enumerate(ids, 1))
            reply += (
                f"\n消息里媒体的 file_id（按出现顺序）：\n{listing}\n"
                "存下来可以复用：下次 photo 块的 media 直接填 file_id，不用再传文件。"
                "复用时整串复制，别手打。"
            )
    return reply


def tool_edit(args: dict[str, Any]) -> str:
    """原地改一条已发出的富消息。

    这是**持久进度窗**的做法：开工先 tg_rich_send 发一条，记住 message_id，
    之后每帧 edit 它——不受草稿 30 秒的限制、留在聊天记录里、编辑不响铃。
    """
    chat = _resolve_chat(args)
    message_id = _int_arg(args, "message_id", "tg_rich_send 返回的那个 id")
    if not message_id:
        # 簿记归脚本：不带 id 就改本会话最后发的那条。跨会话不记——
        # 重启后凭记忆去改一条旧消息，比报错要求给 id 危险得多。
        message_id = _LAST_SENT.get(chat, 0)
        if not message_id:
            raise ValueError(
                "要改哪条？给 message_id，或先用 tg_rich_send 发一条"
                "（本会话发过之后，不带 id 默认改最后那条）"
            )
    rich = build_rich(args)
    call_api("editMessageText", {
        "chat_id": chat,
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
    data: dict[str, Any] = {
        "chat_id": chat,
        "draft_id": draft_id,
        "rich_message": json.dumps(rich, ensure_ascii=False),
    }
    # can_stop（Bot API 10.3）：给用户一颗停止按钮，解锁被流式占住的输入框。
    # ⚠️ 只开按钮不接事件＝半套：用户按停后服务端**照收**后续帧（2026-09-04
    # Android 实测），bot 不监听 stopped_message_generation 就会瞎推到底。
    # 本 server 走 stdio、不碰 getUpdates，接事件是收信侧（频道插件/webhook）的活。
    if args.get("can_stop"):
        data["can_stop"] = "true"
        if args.get("keep_on_stop"):
            data["keep_on_stop"] = "true"
    call_api("sendRichMessageDraft", data)
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
    if name == "tg_sticker_send":
        # 清单模式不该被「没配 chat_id」拦住——真要发的时候模块里再验
        chat = str(args.get("chat_id") or "").strip() or _default_chat()
        return _text(tg_sticker.tool_sticker_send(args, chat, _token(), call_api))
    if name == "tg_sticker_import":
        return _text(tg_sticker.tool_sticker_import(args, _token(), call_api, download_file))
    if name == "tg_ask_choice":
        return _text(tg_ask.tool_ask_choice(args, _resolve_chat(args), call_api))
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
    "每帧 tg_rich_edit 改它。\n"
    "⑧ 本地图九宫格（media_paths 配合 attach://）：\n"
    '   media_paths=["/a/1.jpg","/a/2.jpg"] + blocks 里 '
    '{"type":"collage","blocks":[{"type":"photo","photo":{"type":"photo","media":"attach://f0"}},'
    '{"type":"photo","photo":{"type":"photo","media":"attach://f1"}}]}\n'
    "   photo 的 media 还可以填 http(s) 外链或以前拿到的 file_id（复用不重传）。\n"
    "⑨ 地图（发个坐标给对方看）：\n"
    '   {"type":"map","location":{"latitude":63.4044,"longitude":-19.0588},'
    '"zoom":12,"width":800,"height":500,"caption":{"text":"Reynisfjara 黑沙滩"}}'
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
            "details(summary·blocks[]·is_open) / anchor(name) / "
            "map(location={latitude,longitude}·zoom 0-24·width·height) / "
            "collage·slideshow(blocks[]=photo 块数组·caption={text,credit}) / "
            "photo·video·audio·animation·voice_note / "
            "thinking(仅 draft 可用)。"
            "媒体块的 media 三种来源：http(s) 外链 / 之前拿到的 file_id（复用不重传）/ "
            "attach://f0（配合 media_paths 上传本地文件，第 i 个路径＝f{i}）。"
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
                "media_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "要上传的本地文件（绝对路径，每个≤50MB，最多 50 个）。"
                        "只配 blocks 用：第 i 个路径在 blocks 里用 attach://f{i} 引用"
                        "（见配方⑧）。发送成功会返回各媒体的 file_id，存下来下次直接填"
                        " file_id 复用，不用重新上传。"
                    ),
                },
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
                    "description": (
                        "要改哪条——tg_rich_send 的返回里给了这个 id。"
                        "**可省略**：不给就改本会话最后发的那条（进度窗循环不用记 id）。"
                    ),
                },
            },
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
                "can_stop": {
                    "type": "boolean",
                    "description": "给用户一颗停止按钮（Bot API 10.3+，客户端也要够新）。"
                                   "⚠️ 用户按停后本 server 收不到通知（stop 事件走收信侧），"
                                   "且服务端照收后续帧——接不了事件就别把停止按钮当承诺。",
                },
                "keep_on_stop": {
                    "type": "boolean",
                    "description": "按停后草稿暂留聊天里（仍会很快消失；要真留住得把"
                                   "半成品用 tg_rich_send 补发成正式消息）。需 can_stop。",
                },
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "tg_sticker_send",
        "description": (
            "从贴纸库挑一张**真贴纸**发进 Telegram（sendSticker 车道，"
            "和图片的 file_id 不通用）。**不带参数＝看馆藏清单**。"
            "emoji 挑张：一个＝那一池里随机（自动避开上次刚发的那张）；"
            "多个＝取交集越写越窄（交集为空/不在库会明确报错，不硬找——"
            "发错脸比不发更糟）。id 直取；query 按标题/描述/标签搜。"
            "file_id 各 bot 独立缓存，本 bot 首次用某张时自动从归档原图上传（懒迁移）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "一个或多个 emoji。多个＝取交集收窄（如两个通常就点名一张）。",
                },
                "id": {"type": "string", "description": "馆藏编号直取（清单里那个数字）。"},
                "query": {"type": "string", "description": "按标题/描述/标签搜，命中里随机挑一张。"},
                "chat_id": {
                    "type": "string",
                    "description": "目标聊天；不给就用默认 chat_id。",
                },
            },
        },
    },
    {
        "name": "tg_sticker_import",
        "description": (
            "把收到的贴纸收进库，之后 tg_sticker_send 和句内（emoji）标记都能用它。"
            "给 file_id（**整串程序化取用，绝不手打**），工具会 getFile 下载原图归档，"
            "用返回的 file_unique_id 认人（跨 bot 恒定；已在库的自动认出）。"
            "带 title+emoji＝直接入库；不带＝先落待认领区并返回原图路径，"
            "**用 Read 看图之后**再调一次（带 file_unique_id + title + emoji）认领——"
            "归档自动化了，审美别自动化，标签要看着图写。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "入站消息里贴纸的 file_id。整串复制，别看着截断的显示手补。",
                },
                "file_unique_id": {
                    "type": "string",
                    "description": "认领待认领区条目时用（第一步的返回里给了）。",
                },
                "title": {"type": "string", "description": "给它起个名（看图后写）。"},
                "emoji": {"type": "string", "description": "主情绪 emoji，清单里显示的那个。"},
                "emojis": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "别名入口。挂得越多越容易命中——负担在库上，不在 agent 脑子里。",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "desc": {"type": "string", "description": "一句话描述图里是什么。"},
                "emoji_hint": {
                    "type": "string",
                    "description": "贴纸作者标的 emoji 线索（入站消息里有就带上，帮之后认领）。",
                },
            },
        },
    },
    {
        "name": "tg_ask_choice",
        "description": (
            "发一道**带按钮的选择题**并**同步等答案**：题干+选项变成 inline keyboard，"
            "对方点一下，工具就返回选了哪个（JSON：index / option / message_id）。"
            "适合：A-E 的题、方案二选一、要不要继续的确认、菜单点单。"
            "布局自动：选项全部 ≤16 字（中文计）→ 文字直接上按钮；"
            "任何一条超线 → 整题切「正文列选项全文 + 1️⃣2️⃣3️⃣ 编号按钮」"
            "（超长按钮会被 Telegram 像素级硬剪、连省略号都没有——真机实测）。"
            "私聊只认聊天对面那个人的点击。默认选完原地收按钮标记所选。"
            "⚠️ 需要**专用 bot**（getUpdates 独占）；等答案期间本工具阻塞，默认最多 600s。"
            "⚠️ 实验性：轮询层目前只有单元测试背书、没跑过实弹，遇到怪事请开 issue。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "题干。按钮布局下选项文字别写进来，按钮上有。",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        f"选项文字，1~{tg_ask.MAX_OPTIONS} 个。"
                        "返回的 option 就是这里的原文。"
                    ),
                },
                "layout": {
                    "type": "string",
                    "enum": ["auto", "buttons", "numbered"],
                    "description": (
                        "留空＝auto，按 16 字线自动挑。"
                        "buttons=文字上按钮；numbered=正文列全文+编号按钮。"
                    ),
                },
                "columns": {
                    "type": "integer",
                    "description": (
                        "buttons 布局每行摆几个。留空自适应：全 ≤3 字一行 5 个"
                        "（A-E 正好一排），≤8 字一行 2 个，再长一行 1 个。"
                    ),
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "等多久（秒）。默认 600，上限 3600；超时明确报错。",
                },
                "mark_answered": {
                    "type": "boolean",
                    "description": (
                        "默认 true：选完原地收按钮、标记所选（防幽灵按钮）。"
                        "false=只发不改，选完那条消息变成什么样由你之后自己 edit。"
                    ),
                },
                "chat_id": {
                    "type": "string",
                    "description": "目标聊天；不给就用默认 chat_id。",
                },
            },
            "required": ["question", "options"],
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
    "发本地图片用 media_paths + blocks 里 attach://f0 引用（多图就是拼贴/轮播）；"
    "发过一次的媒体存 file_id 复用，不用重新上传。"
    "写内容前先看一眼 blocks 参数描述末尾的配方。\n"
    "贴纸车道（库非空才生效）：tg_rich_send 的 markdown 正文里写（emoji）＝"
    "那个位置发一张库里的真贴纸——一个 emoji 随机挑、多个取交集点名；"
    "认不出就原样留在文字里，不穿帮。想精确控制用 tg_sticker_send；"
    "收到没见过的贴纸用 tg_sticker_import 归档，看图起标题配标签后认领入库。\n"
    "要对方**点按钮回答**（选择题、二选一、要不要继续）用 tg_ask_choice——"
    "工具同步等点击、直接返回选了哪个，不用自己盯回流。"
    "⚠️ 它要**专用 bot**：getUpdates 全 Telegram 只许一个消费者，"
    "bot 同时挂着官方插件/webhook 会 409。"
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
