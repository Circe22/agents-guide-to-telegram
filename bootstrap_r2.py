#!/usr/bin/env python3
"""Temporary PR bootstrap for R2 security hardening.

The first PR CI imports this via test_000_apply_r2_patch.py, patches the checkout
in-place, and tests the patched tree. After every Linux/Windows test job passes,
a finalize job applies the same patch to the real head branch, writes permanent
regression tests, restores the normal workflow, and deletes this bootstrap.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "tg_rich_mcp.py"
README = ROOT / "README.md"
FINAL_TEST = ROOT / "test_security_boundaries_r2.py"


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def apply_source_patch() -> bool:
    s = SOURCE.read_text(encoding="utf-8")
    if "REQUEST_MAX_BYTES_DEFAULT" in s and "def _guard_semantic_string(" in s:
        return False

    request_guard = '''# ---------- JSON-RPC 传输层字节闸 ----------
REQUEST_MAX_BYTES_DEFAULT = 4 * 1024 * 1024
REQUEST_DRAIN_CHUNK = 64 * 1024


def _request_max_bytes() -> int:
    """单条 JSON-RPC 请求最大字节数；坏值/非正数回落 4 MiB。"""
    raw = (os.environ.get("TG_RICH_MAX_REQUEST_BYTES") or "").strip()
    try:
        value = int(raw) if raw else REQUEST_MAX_BYTES_DEFAULT
    except ValueError:
        return REQUEST_MAX_BYTES_DEFAULT
    return value if value > 0 else REQUEST_MAX_BYTES_DEFAULT


def _read_request_line(stream: Any, limit: int) -> tuple[Any | None, bool]:
    """有界读一条换行分隔请求，返回 ``(line, oversized)``。

    首次最多分配 ``limit + 1``；若超限，用固定 64 KiB 窗口把这一行剩余
    部分 drain 到换行/EOF，再继续下一条。生产路径传 ``sys.stdin.buffer``，
    因此限制按**字节**生效，且拒绝发生在 ``json.loads`` 之前。
    """
    line = stream.readline(limit + 1)
    if line == b"" or line == "":
        return None, False
    if len(line) <= limit:
        return line, False

    newline = b"\\n" if isinstance(line, (bytes, bytearray)) else "\\n"
    empty = b"" if isinstance(line, (bytes, bytearray)) else ""
    if not line.endswith(newline):
        while True:
            tail = stream.readline(REQUEST_DRAIN_CHUNK)
            if not tail or tail.endswith(newline):
                break
    return empty, True


'''
    s = _replace_once(
        s, "# ---------- 媒体上传 ----------", request_guard + "# ---------- 媒体上传 ----------",
        "request-byte-guard insertion",
    )

    old_guard = '''def _guard_string(value: str, media_count: int) -> None:
    """字符串值的安全闸：attach:// 索引越界、file:// 一类本地 scheme。"""
    match = _ATTACH_RE.match(value)
    if match:
        idx = int(match.group(1))
        if idx >= media_count:
            if media_count:
                raise ValueError(
                    f"attach://f{idx} 引用了不存在的媒体——"
                    f"media_paths 只有 {media_count} 个文件（f0..f{media_count - 1}）"
                )
            raise ValueError(
                f"attach://f{idx} 引用了媒体，但这次没有提供 media_paths"
            )
        return
    stripped = value.strip().lower()
    for scheme in _LOCAL_URL_SCHEMES:
        if stripped.startswith(scheme + ":"):
            raise ValueError(f"URL 用了本地 scheme（{scheme}:），不发")
'''
    new_guard = '''def _guard_semantic_string(value: str, field: str | None, media_count: int) -> None:
    """只解释有明确 URL/媒体语义的字段；普通 text/pre/code 字面量不碰。"""
    if field == "media":
        match = _ATTACH_RE.fullmatch(value)
        if match:
            idx = int(match.group(1))
            if idx >= media_count:
                if media_count:
                    raise ValueError(
                        f"attach://f{idx} 引用了不存在的媒体——"
                        f"media_paths 只有 {media_count} 个文件（f0..f{media_count - 1}）"
                    )
                raise ValueError(
                    f"attach://f{idx} 引用了媒体，但这次没有提供 media_paths"
                )
            return
    if field in ("media", "url"):
        stripped = value.strip().lower()
        for scheme in _LOCAL_URL_SCHEMES:
            if stripped.startswith(scheme + ":"):
                raise ValueError(f"URL 用了本地 scheme（{scheme}:），不发")
'''
    s = _replace_once(s, old_guard, new_guard, "semantic-string guard")

    s = _replace_once(
        s,
        "    stack: list[tuple[Any, int]] = [(blocks, 0)]",
        "    stack: list[tuple[Any, int, str | None]] = [(blocks, 0, None)]",
        "guard stack shape",
    )
    s = _replace_once(
        s,
        "    def _push(item: Any, depth: int) -> None:",
        "    def _push(item: Any, depth: int, field: str | None = None) -> None:",
        "guard push signature",
    )
    s = _replace_once(
        s,
        "        stack.append((item, depth))",
        "        stack.append((item, depth, field))",
        "guard push tuple",
    )
    s = _replace_once(
        s,
        "        node, depth = stack.pop()",
        "        node, depth, field = stack.pop()",
        "guard pop tuple",
    )
    s = _replace_once(
        s,
        "                _push(value, depth + 1)     # 逐个计数入栈，扩栈有界（R1-2）",
        "                _push(value, depth + 1, key)  # 带字段语义；资源预算仍覆盖所有值",
        "dict field propagation",
    )
    s = _replace_once(
        s,
        "                _push(item, depth + 1)",
        "                _push(item, depth + 1, field)",
        "list field propagation",
    )
    s = _replace_once(
        s,
        "        elif isinstance(node, str):\n            _account_string(node)\n            _guard_string(node, media_count)",
        "        elif isinstance(node, str):\n            _account_string(node)\n            _guard_semantic_string(node, field, media_count)",
        "semantic guard call",
    )

    s = _replace_once(
        s,
        "def build_rich(args: dict[str, Any]) -> dict[str, Any]:",
        "def build_rich(args: dict[str, Any], *, media_count: int = 0) -> dict[str, Any]:",
        "build_rich capability signature",
    )
    s = _replace_once(
        s,
        '''    # attach://fN 的 N 必须落在实际 media_paths 索引内——数量在这儿就能拿到。\n    media_paths = args.get("media_paths")\n    media_count = len(media_paths) if isinstance(media_paths, list) else 0\n\n''',
        "",
        "remove implicit media capability",
    )
    old_parse = '''        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except json.JSONDecodeError as exc:
                raise ValueError(f"blocks 不是合法 JSON：{exc}") from None
'''
    new_parse = '''        if isinstance(blocks, str):
            # string 形式也必须在第二次 json.loads **之前**有界；stdio 主路径外的
            # 直接调用同样不能先吞一个无界 JSON string 再事后 guard。
            if len(blocks.encode("utf-8")) > _request_max_bytes():
                raise ValueError(
                    "blocks JSON string 超过解析前字节上限（TG_RICH_MAX_REQUEST_BYTES）"
                )
            try:
                blocks = json.loads(blocks)
            except json.JSONDecodeError as exc:
                raise ValueError(f"blocks 不是合法 JSON：{exc}") from None
'''
    s = _replace_once(s, old_parse, new_parse, "string-form preparse bound")

    old_send = '''def tool_send(args: dict[str, Any]) -> str:
    # 公共校验先行：三选一的约束在**任何发送之前**守（build_rich），命中贴纸
    # 分支不该改变参数合同——同样的输入不能因库里有没有那张脸而校验结果不同（B7）。
    rich = build_rich(args)
'''
    new_send = '''def tool_send(args: dict[str, Any]) -> str:
    # media_count 是“本调用真的会上传这些附件”的 capability，不从任意 args 隐式推导。
    media_paths = args.get("media_paths")
    if media_paths is None:
        media_count = 0
    elif isinstance(media_paths, list) and all(isinstance(p, str) for p in media_paths):
        media_count = len(media_paths)
    else:
        raise ValueError("media_paths 必须是字符串数组（本地文件的绝对路径）")

    # 公共校验先行：三选一的约束在**任何发送之前**守（build_rich），命中贴纸
    # 分支不该改变参数合同——同样的输入不能因库里有没有那张脸而校验结果不同（B7）。
    rich = build_rich(args, media_count=media_count)
'''
    s = _replace_once(s, old_send, new_send, "tool_send media capability")
    s = _replace_once(
        s, '    if markdown_raw.strip() and not args.get("media_paths"):',
        '    if markdown_raw.strip() and not media_paths:', "tool_send marker media check",
    )
    s = _replace_once(
        s, '    if args.get("media_paths"):', '    if media_paths:', "tool_send media branch",
    )
    s = _replace_once(
        s, '        media = load_media(args["media_paths"])', '        media = load_media(media_paths)',
        "tool_send media load",
    )

    edit_anchor = '''    这是**持久进度窗**的做法：开工先 tg_rich_send 发一条，记住 message_id，
    之后每帧 edit 它——不受草稿 30 秒的限制、留在聊天记录里、编辑不响铃。
    """
    chat = _resolve_chat(args)
'''
    edit_new = '''    这是**持久进度窗**的做法：开工先 tg_rich_send 发一条，记住 message_id，
    之后每帧 edit 它——不受草稿 30 秒的限制、留在聊天记录里、编辑不响铃。
    """
    if "media_paths" in args:
        raise ValueError("media_paths 只支持 tg_rich_send；tg_rich_edit 不上传附件")
    chat = _resolve_chat(args)
'''
    s = _replace_once(s, edit_anchor, edit_new, "tool_edit media rejection")
    s = _replace_once(
        s,
        '''def tool_draft(args: dict[str, Any]) -> str:\n    draft_id = _int_arg(args, "draft_id", "任意非零整数，同一个 id 才会做动画过渡")''',
        '''def tool_draft(args: dict[str, Any]) -> str:\n    if "media_paths" in args:\n        raise ValueError("media_paths 只支持 tg_rich_send；tg_rich_draft 不上传附件")\n    draft_id = _int_arg(args, "draft_id", "任意非零整数，同一个 id 才会做动画过渡")''',
        "tool_draft media rejection",
    )

    old_main = '''def main() -> None:
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
                json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\\n"
            )
            sys.stdout.flush()
'''
    new_main = '''def main() -> None:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    limit = _request_max_bytes()
    while True:
        raw_line, oversized = _read_request_line(stream, limit)
        if raw_line is None:
            break
        message: Any = None
        if oversized:
            response = _error(
                None, -32600,
                f"request exceeds {limit} byte limit (TG_RICH_MAX_REQUEST_BYTES)",
            )
        else:
            try:
                if isinstance(raw_line, (bytes, bytearray)):
                    line = bytes(raw_line).decode("utf-8").strip()
                else:
                    line = str(raw_line).strip()
                if not line:
                    continue
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("request must be an object")
                response = handle(message)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                response = _error(None, -32700, str(exc))
            except Exception as exc:
                # 单条消息的最终兜底：任何没预料到的异常都不许掀掉读循环。
                response = _error(
                    message.get("id") if isinstance(message, dict) else None,
                    -32603,
                    f"internal error: {type(exc).__name__}",
                )
        if response is not None:
            sys.stdout.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\\n"
            )
            sys.stdout.flush()
'''
    s = _replace_once(s, old_main, new_main, "bounded main loop")

    SOURCE.write_text(s, encoding="utf-8")
    return True


def write_final_tests() -> None:
    FINAL_TEST.write_text('''import io
import json
import os
import tempfile
import unittest
from unittest import mock

import tg_rich_mcp as mcp


class SemanticStringScope(unittest.TestCase):
    def test_plain_text_literals_are_not_interpreted(self):
        mcp.guard_blocks([
            {"type": "pre", "text": "file:///etc/passwd"},
            {"type": "paragraph", "text": "example attach://f999999 only"},
        ])

    def test_media_attach_still_checked(self):
        with self.assertRaisesRegex(ValueError, "attach"):
            mcp.guard_blocks(
                [{"type": "photo", "photo": {"media": "attach://f3"}}], media_count=1
            )

    def test_media_and_url_file_scheme_still_rejected(self):
        cases = (
            [{"type": "photo", "photo": {"media": "file:///etc/passwd"}}],
            [{"type": "paragraph", "text": [
                {"type": "url", "text": "x", "url": "file:///etc/passwd"}
            ]}],
        )
        for blocks in cases:
            with self.subTest(blocks=blocks):
                with self.assertRaisesRegex(ValueError, "本地 scheme"):
                    mcp.guard_blocks(blocks)

    def test_valid_media_attach_requires_explicit_capability(self):
        blocks = [{"type": "photo", "photo": {"media": "attach://f0"}}]
        mcp.build_rich({"blocks": blocks}, media_count=1)
        with self.assertRaisesRegex(ValueError, "attach"):
            mcp.build_rich({"blocks": blocks})


class MediaCapabilityBoundary(unittest.TestCase):
    def test_edit_cannot_fake_media_paths(self):
        args = {
            "chat_id": "10001", "message_id": 1,
            "blocks": [{"type": "photo", "photo": {"media": "attach://f0"}}],
            "media_paths": ["/tmp/not-really-uploaded.jpg"],
        }
        with mock.patch.object(mcp, "call_api") as api:
            with self.assertRaisesRegex(ValueError, "只支持 tg_rich_send"):
                mcp.tool_edit(args)
            api.assert_not_called()

    def test_draft_cannot_fake_media_paths(self):
        args = {
            "chat_id": "10001", "draft_id": 1,
            "blocks": [{"type": "photo", "photo": {"media": "attach://f0"}}],
            "media_paths": ["/tmp/not-really-uploaded.jpg"],
        }
        with mock.patch.object(mcp, "call_api") as api:
            with self.assertRaisesRegex(ValueError, "只支持 tg_rich_send"):
                mcp.tool_draft(args)
            api.assert_not_called()

    def test_send_real_media_path_authorizes_attach(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as fh:
            fh.write(b"jpg")
            fh.flush()
            args = {
                "chat_id": "10001",
                "blocks": [{"type": "photo", "photo": {"media": "attach://f0"}}],
                "media_paths": [fh.name],
            }
            with mock.patch.object(
                mcp, "call_api", return_value={"result": {"message_id": 7}}
            ) as api:
                mcp.tool_send(args)
            self.assertEqual(api.call_args.args[0], "sendRichMessage")
            self.assertIn("f0", api.call_args.kwargs["files"])


class TransportByteBound(unittest.TestCase):
    def test_oversized_line_is_drained_and_next_request_survives(self):
        stream = io.BytesIO(b"x" * 200_000 + b"\\n" + b'{"jsonrpc":"2.0"}\\n')
        line, oversized = mcp._read_request_line(stream, 64)
        self.assertTrue(oversized)
        self.assertEqual(line, b"")
        line, oversized = mcp._read_request_line(stream, 64)
        self.assertFalse(oversized)
        self.assertEqual(line, b'{"jsonrpc":"2.0"}\\n')

    def test_limit_counts_utf8_bytes(self):
        stream = io.BytesIO("猫猫\\n".encode("utf-8"))
        _line, oversized = mcp._read_request_line(stream, 6)
        self.assertTrue(oversized)

    def test_main_returns_error_then_continues(self):
        oversized = b"x" * 100 + b"\\n"
        ping = b'{"jsonrpc":"2.0","id":9,"method":"ping"}\\n'

        class FakeStdin:
            def __init__(self, data: bytes):
                self.buffer = io.BytesIO(data)

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "64"}), \\
             mock.patch.object(mcp.sys, "stdin", FakeStdin(oversized + ping)), \\
             mock.patch.object(mcp.sys, "stdout", out):
            mcp.main()
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["id"], 9)
        self.assertEqual(responses[1]["result"], {})

    def test_blocks_string_is_bounded_before_second_json_parse(self):
        raw = '[{"type":"paragraph","text":"' + ("x" * 1000) + '"}]'
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "128"}), \\
             mock.patch.object(mcp.json, "loads", wraps=mcp.json.loads) as loads:
            with self.assertRaisesRegex(ValueError, "解析前字节上限"):
                mcp.build_rich({"blocks": raw})
            loads.assert_not_called()

    def test_bad_env_falls_back(self):
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "nope"}):
            self.assertEqual(mcp._request_max_bytes(), mcp.REQUEST_MAX_BYTES_DEFAULT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
''', encoding="utf-8")


def patch_readme() -> None:
    r = README.read_text(encoding="utf-8")
    old_intro = (
        "- **出站三道有界闸**（见下表）：blocks 结构有界、媒体目录可限、目标 chat 可限。\n"
        "  目标是 **bounding，不是复刻 Telegram 的 schema**——未知 block type 照样透传，\n"
        "  我们不维护一份会跟 Telegram 漂移的白名单。"
    )
    new_intro = (
        "- **四道边界闸**（见下表）：JSON-RPC 请求先限字节，blocks 再限结构，媒体目录与目标 chat 均可收紧。\n"
        "  目标是 **bounding，不是复刻 Telegram 的 schema**——未知 block type 照样透传，\n"
        "  我们不维护一份会跟 Telegram 漂移的白名单。"
    )
    r = _replace_once(r, old_intro, new_intro, "README trust-model intro")

    lines = []
    saw_blocks = False
    saw_media = False
    for line in r.splitlines():
        if line.startswith("| blocks 有界结构闸 |"):
            lines.append(
                "| JSON-RPC 请求字节闸 | `TG_RICH_MAX_REQUEST_BYTES` | 4 MiB | 在 `json.loads` **之前**按字节有界读取；超长行只保留 `limit+1`，其余用固定窗口 drain 到换行后继续服务，避免巨型请求先把 parser/内存吃满。 |"
            )
            lines.append(
                "| blocks 有界结构闸 | 常开；`TG_RICH_BLOCKS_MAX_NODES` / `TG_RICH_BLOCKS_MAX_CHARS` 可调 | 深度 16 / 节点 2000 / 单数组 4096 / 单串 10 万 / 总字符 100 万 | 迭代遍历框住 blocks 的深度/节点/数组/字符串/总字符；dict 键必须是 str、只放行 JSON 兼容类型。`attach://` / `file:` 只在真正的 `media` / `url` 字段解释，普通 `text` / `pre` 里的字面量不误杀；map 经纬度·缩放仍就地判死。 |"
            )
            saw_blocks = True
        elif line.startswith("| 媒体目录白名单 |"):
            lines.append(
                "| 媒体目录白名单 | `TG_RICH_MEDIA_ROOTS`（多目录按 `os.pathsep` 分隔：**Unix `:` / Windows `;`**） | **未配＝不限目录** | 稳定路径下按 `resolve()` 后真实目标做父子判定，普通 symlink 越界会被拦；凭证文件名 guard 仍作第二层。**这不是 race-hard filesystem sandbox**：不承诺抵抗另一个本地进程在检查与 `open()` 之间并发替换路径组件（TOCTOU）。 |"
            )
            saw_media = True
        else:
            lines.append(line)
    if not (saw_blocks and saw_media):
        raise RuntimeError(f"README table anchors missing: blocks={saw_blocks}, media={saw_media}")
    README.write_text("\n".join(lines) + "\n", encoding="utf-8")


def finalize() -> None:
    apply_source_patch()
    write_final_tests()
    patch_readme()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.finalize:
        finalize()
    else:
        apply_source_patch()


if __name__ == "__main__":
    main()
