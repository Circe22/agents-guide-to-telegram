#!/usr/bin/env python3
"""tg-rich-mcp 的测试。

跑：
    python3 -m unittest discover -v          # 或 python3 test_tg_rich_mcp.py

**一条网络请求都不发**，也不读你的真配置——`HOME` 全程指向临时目录。
所以随便跑，不会往你的 Telegram 里发东西。

测的是那些"坏了会伤到别人"的地方：
  · 脱敏（坏了＝把密钥推进聊天里）
  · 并发写状态（坏了＝进度窗丢行）
  · 帧序（坏了＝窗口倒退）
  · 三选一校验（坏了＝API 拒收，用户莫名其妙）
  · 错误分类（坏了＝模型看不见原因，不会重试）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import secret_redaction  # noqa: E402
import tg_rich_mcp as mcp  # noqa: E402

# 进度窗 hook 是 Unix-only（fcntl），Windows 上 import 就炸。
# server 核心（tg_rich_mcp/tg_sticker/tg_ask/secret_redaction）承诺全平台，
# 所以 hook 的测试按平台跳过、其余照跑——Windows CI 靠这一层过活。
try:
    import tg_progress_hook as hook  # noqa: E402
except ImportError:                  # pragma: no cover - 只在 Windows 走到
    hook = None

needs_hook = unittest.skipIf(hook is None, "tg_progress_hook 是 Unix-only（fcntl）")

FAKE_TOKEN = "123456789:AAH" + "x" * 32
LEAK_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"


def rpc(method: str, params: dict | None = None, request_id: int = 1):
    return mcp.handle({"jsonrpc": "2.0", "id": request_id,
                       "method": method, "params": params or {}})


def call_tool(name: str, args: dict):
    return rpc("tools/call", {"name": name, "arguments": args})


class ContentFieldValidation(unittest.TestCase):
    """markdown / html / blocks —— 官方原文 Exactly one。"""

    def test_exactly_one_accepted(self):
        for field, value in [("markdown", "# hi"), ("html", "<b>hi</b>"),
                             ("blocks", [{"type": "paragraph", "text": "hi"}])]:
            with self.subTest(field=field):
                rich = mcp.build_rich({field: value})
                self.assertEqual(list(rich), [field])

    def test_zero_rejected(self):
        with self.assertRaises(ValueError):
            mcp.build_rich({})

    def test_two_rejected(self):
        with self.assertRaises(ValueError):
            mcp.build_rich({"markdown": "x", "html": "<b>y</b>"})

    def test_blocks_accepts_json_string(self):
        """agent 经常把 blocks 序列化成字符串传进来，得认。"""
        rich = mcp.build_rich({"blocks": '[{"type":"paragraph","text":"hi"}]'})
        self.assertEqual(rich["blocks"][0]["type"], "paragraph")

    def test_blocks_must_be_a_list(self):
        with self.assertRaises(ValueError):
            mcp.build_rich({"blocks": '{"type":"paragraph"}'})


class Redaction(unittest.TestCase):
    """出口脱敏。坏了就是把 token 递出去。"""

    def test_bot_token_shape_scrubbed(self):
        """真实泄漏长这样——token 紧贴在 "bot" 后面，中间没有词边界。"""
        out = mcp._scrub_out(f"connection failed to {LEAK_URL}")
        self.assertNotIn("AAHx", out)
        self.assertIn("<token>", out)

    def test_bare_and_prefixed_forms(self):
        for text in [FAKE_TOKEN, f"token={FAKE_TOKEN}", f"bot{FAKE_TOKEN}", LEAK_URL]:
            with self.subTest(text=text[:30]):
                self.assertNotIn("AAHx", mcp._scrub_out(text))

    def test_ordinary_numbers_survive(self):
        """别为了认 token 把正常内容也抹了。"""
        for benign in ["chat_id=-1001234567890", "timestamp=1722300000",
                       "version=2025-06-18", "status 123456:short"]:
            with self.subTest(benign=benign):
                self.assertEqual(mcp._scrub_out(benign), benign)

    @needs_hook
    def test_fallback_copy_matches_source_of_truth(self):
        """盯的必须是**副本的字面量**。

        比 `hook._TELEGRAM_TOKEN_RE.pattern` 是没用的：测试环境下 import 一定成功，
        那个名字就是共享对象本身，等于断言 A == A——副本改回 `\\b\\d{8,}` 也照绿。
        """
        self.assertEqual(hook._FALLBACK_TELEGRAM_TOKEN_PATTERN,
                         secret_redaction.TELEGRAM_BOT_TOKEN_RE.pattern)

    @needs_hook
    def test_fallback_copy_actually_works(self):
        """副本自己也得真拦得住，不能只是长得一样。"""
        fallback = __import__("re").compile(hook._FALLBACK_TELEGRAM_TOKEN_PATTERN)
        self.assertTrue(fallback.search(LEAK_URL))
        self.assertFalse(fallback.search("chat_id=-1001234567890"))

    def test_scrub_is_idempotent(self):
        once = mcp._scrub_out("bot987654321:BB" + "y" * 33)
        self.assertEqual(once, mcp._scrub_out(once))

    def test_int_arg_never_echoes_the_value(self):
        """校验异常也是出口：调用方可能刚好把 token 填错了位置。"""
        token_like = "123456789:AA" + "z" * 33
        with self.assertRaises(ValueError) as ctx:
            mcp._int_arg({"draft_id": token_like}, "draft_id", "任意非零整数")
        self.assertNotIn(token_like, str(ctx.exception))


class ErrorClassification(unittest.TestCase):
    """协议错误 vs 执行错误——分错了模型就不会自己重试。"""

    def test_unknown_tool_is_protocol_error(self):
        self.assertIn("error", call_tool("no_such_tool", {}))

    def test_arguments_must_be_object(self):
        reply = rpc("tools/call", {"name": "tg_rich_send", "arguments": []})
        self.assertIn("error", reply)

    def test_validation_failure_is_tool_error(self):
        reply = call_tool("tg_rich_send", {"markdown": "x", "html": "<b>y</b>"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("三选一", reply["result"]["content"][0]["text"])

    def test_missing_message_id_is_tool_error(self):
        # 本会话没发过任何消息时，缺 id 必须还是错误（edit-last 只在发过之后接管）
        mcp._LAST_SENT.clear()
        reply = call_tool("tg_rich_edit", {"markdown": "x"})
        self.assertTrue(reply["result"]["isError"])

    def test_tool_error_text_is_scrubbed(self):
        """执行错误也是出口。"""
        marker = "555555555:CC" + "w" * 33
        self.assertNotIn(marker, json.dumps(
            mcp._text_error(f"boom {marker}"), ensure_ascii=False))


class Protocol(unittest.TestCase):
    def test_version_negotiation(self):
        for wanted in mcp.SUPPORTED_PROTOCOLS:
            with self.subTest(wanted=wanted):
                reply = rpc("initialize", {"protocolVersion": wanted})
                self.assertEqual(reply["result"]["protocolVersion"], wanted)

    def test_unknown_version_falls_back(self):
        reply = rpc("initialize", {"protocolVersion": "2099-01-01"})
        self.assertEqual(reply["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_params_array_is_rejected_as_wrong_type(self):
        """合法 JSON 但 params 是数组——以前这里 AttributeError 掀掉整个 server。

        断言要盯到**具体错误**：只查 "error" in reply 的话，`params or {}` 那种
        静默转换也会让它绿（最后因为工具名为空报了 unknown tool，理由完全不对）。
        """
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1,
                            "method": "tools/call", "params": []})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(reply["error"]["message"], "params must be an object")

    def test_falsy_params_are_not_silently_accepted(self):
        for bad in ["", 0, []]:
            with self.subTest(bad=repr(bad)):
                reply = mcp.handle({"jsonrpc": "2.0", "id": 1,
                                    "method": "tools/call", "params": bad})
                self.assertEqual(reply["error"]["message"], "params must be an object")

    def test_notifications_get_no_reply(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0",
                                      "method": "notifications/initialized"}))

    def test_known_method_without_id_gets_no_reply(self):
        """没有 id ＝ notification。以前会先执行、再回一条 id=null 的响应；
        更糟的是 tools/call 当 notification 发进来——消息真发出去，调用方拿不到结果。"""
        for method in ["tools/list", "initialize", "ping"]:
            with self.subTest(method=method):
                self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": method}))

    def test_tools_call_as_notification_does_not_execute(self):
        sent = []
        original, mcp.call_api = mcp.call_api, lambda m, d: sent.append(m)
        try:
            reply = mcp.handle({"jsonrpc": "2.0", "method": "tools/call",
                                "params": {"name": "tg_rich_send",
                                           "arguments": {"markdown": "hi"}}})
        finally:
            mcp.call_api = original
        self.assertIsNone(reply)
        self.assertEqual(sent, [], "notification 不该真把消息发出去")

    def test_tools_list_matches_known_tools(self):
        names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, set(mcp.KNOWN_TOOLS))
        self.assertEqual(len(names), 6)   # rich 三件 + 贴纸两件 + 按钮选择题一件


@needs_hook
class ToolSummaries(unittest.TestCase):
    """进度窗只推工具名 + 一句安全摘要，绝不推内容。"""

    def test_bash_uses_description_not_command(self):
        line = hook._line("Bash", {"command": "curl -H 'Authorization: Bearer abc'",
                                   "description": "拉一下接口"})
        self.assertIn("拉一下接口", line)
        self.assertNotIn("Authorization", line)

    def test_read_shows_basename_only(self):
        line = hook._line("Read", {"file_path": "/home/someone/vault/notes.md"})
        self.assertIn("notes.md", line)
        self.assertNotIn("/home/someone", line)

    def test_filename_itself_can_be_the_secret(self):
        self.assertIn("内容隐去",
                      hook._line("Read", {"file_path": "/tmp/AKIAIOSFODNN7EXAMPLE"}))

    def test_webfetch_drops_query_and_userinfo(self):
        line = hook._line("WebFetch", {"url": "https://u:p@example.com/a?token=zzz"})
        self.assertNotIn("token=zzz", line)
        self.assertNotIn("u:p", line)

    def test_keyword_gate(self):
        self.assertIn("内容隐去", hook._line("Bash", {"description": "读 .env 文件"}))

    def test_shape_gate_catches_keyless_secrets(self):
        """关键词闸拦不住这些——它们一个关键词都没有。"""
        for secret in ["deploy sk-live-ABC123XYZdef", "AKIAIOSFODNN7EXAMPLE",
                       "ghp_" + "a" * 20, "eyJhbGciOiJIUzI1NiJ9.payload",
                       "-----BEGIN RSA PRIVATE KEY"]:
            with self.subTest(secret=secret):
                self.assertIn("内容隐去",
                              hook._line("Bash", {"description": secret}))

    def test_shape_gate_catches_telegram_token_inside_url(self):
        """进度窗这侧漏过一次：MCP 的闸修好了，这道还敞着。"""
        line = hook._line("Bash", {"description": f"request failed: {LEAK_URL}"})
        self.assertIn("内容隐去", line)
        self.assertNotIn(FAKE_TOKEN, line)

    def test_shape_gate_does_not_hurt_normal_numbers(self):
        for ordinary in ["chat_id=-1001234567890", "timestamp=1722300000",
                         "version=2025-06-18", "status 123456:short"]:
            with self.subTest(ordinary=ordinary):
                self.assertNotIn("内容隐去",
                                 hook._line("Bash", {"description": ordinary}))

    def test_grep_pattern_only_when_it_looks_ordinary(self):
        self.assertIn("handleRequest", hook._line("Grep", {"pattern": "handleRequest"}))
        self.assertNotIn("sk-live",
                         hook._line("Grep", {"pattern": "sk-live-ABC123XYZdef"}))

    def test_redaction_can_be_turned_off(self):
        """闸必须能关——这是它的用户要求，不是可选项。"""
        os.environ["TG_PROGRESS_REDACT"] = "0"
        try:
            self.assertIn("读 .env 文件",
                          hook._line("Bash", {"description": "读 .env 文件"}))
        finally:
            os.environ.pop("TG_PROGRESS_REDACT", None)


@needs_hook
class ProgressBlocks(unittest.TestCase):
    def test_thinking_block_only_in_draft(self):
        """thinking 进不了正式消息，照搬会被 API 拒收。"""
        kinds = [b["type"] for b in hook._blocks(["a"], 1, draft=True)]
        self.assertIn("thinking", kinds)
        for blocks in [hook._blocks(["a"], 1), hook._blocks(["a"], 1, done=True)]:
            kinds = [b["type"] for b in blocks]
            self.assertNotIn("thinking", kinds)
            self.assertIn("footer", kinds)

    def test_step_count_is_not_the_truncated_line_count(self):
        """lines 截断在 40，拿 len(lines) 当步数会永远显示 40 步。"""
        footer = hook._blocks([f"step {i}" for i in range(40)], 137)[-1]
        self.assertIn("137", footer["text"])

    def test_empty_lines_produce_no_empty_list_block(self):
        kinds = [b["type"] for b in hook._blocks([], 0)]
        self.assertNotIn("list", kinds)

    def test_session_id_cannot_escape_the_state_dir(self):
        """session_id 是外来输入，直接拼路径能用 ../ 跑出去写别处。"""
        evil = hook._state_path("../../../../tmp/pwned")
        self.assertEqual(evil.parent, hook.STATE_DIR)
        self.assertNotIn("..", str(evil))


class EditLast(unittest.TestCase):
    """edit 不带 message_id 默认改本会话最后发的那条——簿记归脚本。"""

    def setUp(self):
        self.calls = []
        self.original = mcp.call_api

        def fake(method, data, files=None):
            self.calls.append((method, dict(data)))
            return {"result": {"message_id": 88}}

        mcp.call_api = fake
        self.had_chat = os.environ.get("TG_CHAT_ID")
        os.environ["TG_CHAT_ID"] = "-100777"
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TG_STICKER_DIR"] = self.tmp.name   # 空库＝标记层不掺和
        mcp._LAST_SENT.clear()

    def tearDown(self):
        mcp.call_api = self.original
        if self.had_chat is None:
            os.environ.pop("TG_CHAT_ID", None)
        else:
            os.environ["TG_CHAT_ID"] = self.had_chat
        os.environ.pop("TG_STICKER_DIR", None)
        mcp._LAST_SENT.clear()
        self.tmp.cleanup()

    def test_edit_without_id_targets_last_sent(self):
        mcp.tool_send({"markdown": "第一帧"})
        out = mcp.tool_edit({"markdown": "第二帧"})
        self.assertEqual(self.calls[-1][0], "editMessageText")
        self.assertEqual(self.calls[-1][1]["message_id"], 88)
        self.assertIn("88", out)

    def test_edit_without_id_before_any_send_errors(self):
        with self.assertRaises(ValueError):
            mcp.tool_edit({"markdown": "x"})

    def test_explicit_id_wins_over_last_sent(self):
        mcp.tool_send({"markdown": "hi"})
        mcp.tool_edit({"markdown": "y", "message_id": "5"})
        self.assertEqual(self.calls[-1][1]["message_id"], 5)

    def test_last_sent_is_per_chat(self):
        mcp.tool_send({"markdown": "给默认 chat"})
        with self.assertRaises(ValueError):
            mcp.tool_edit({"markdown": "x", "chat_id": "另一个聊天"})


@needs_hook
class FrameOrdering(unittest.TestCase):
    """慢帧不能把新内容盖回去（窗口倒退）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.calls = []
        self.original = mcp.call_api
        mcp.call_api = lambda method, data: (
            self.calls.append(method), {"result": {"message_id": 42}})[1]
        # 没有 chat_id 的话 _push 会在发请求之前就早退，这几条就测不到东西了
        self.had_chat = os.environ.get("TG_CHAT_ID")
        os.environ["TG_CHAT_ID"] = "-100999"

    def tearDown(self):
        mcp.call_api = self.original
        if self.had_chat is None:
            os.environ.pop("TG_CHAT_ID", None)
        else:
            os.environ["TG_CHAT_ID"] = self.had_chat
        self.tmp.cleanup()

    def write(self, state):
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def test_stale_frame_yields(self):
        self.write({"msg_id": 42, "seq": 9, "lines": ["new"], "total": 9})
        hook._push(self.path, seq=3)          # 出发时是第 3 帧，落地时已经第 9 帧
        self.assertEqual(self.calls, [])

    def test_current_frame_edits(self):
        self.write({"msg_id": 42, "seq": 9, "lines": ["new"], "total": 9})
        hook._push(self.path, seq=9)
        self.assertEqual(self.calls, ["editMessageText"])

    def test_slow_old_frame_cannot_land_after_a_newer_one(self):
        """光在出发前查 seq 挡不住这个：查完之后、请求返回之前，新帧可能已经发完了。

        这里让旧帧的请求很慢、新帧的很快——没有推送锁的话，新帧先落地、
        旧帧后落地，窗口就倒退回旧内容了。
        """
        import threading
        import time

        order = []

        def timed_api(method, data):
            payload = json.loads(data["rich_message"])
            tag = payload["blocks"][1]["items"][0]["blocks"][0]["text"]
            time.sleep(0.30 if tag == "frame-1" else 0.01)
            order.append(tag)
            return {"result": {"message_id": 42}}

        mcp.call_api = timed_api
        self.write({"msg_id": 42, "seq": 1, "lines": ["frame-1"], "total": 1})
        old = threading.Thread(target=hook._push, args=(self.path, 1))
        old.start()
        time.sleep(0.05)                       # 让慢的那帧先进锁
        self.write({"msg_id": 42, "seq": 2, "lines": ["frame-2"], "total": 2})
        new = threading.Thread(target=hook._push, args=(self.path, 2))
        new.start()
        old.join(timeout=10)
        new.join(timeout=10)

        self.assertTrue(order, "一帧都没发出去")
        self.assertEqual(order[-1], "frame-2", "旧帧最后落地＝窗口倒退")

    def test_only_the_claiming_frame_opens_the_window(self):
        """并发的几帧都发现没有 msg_id 时，只有被派活的那帧能发。"""
        self.write({"seq": 5, "claim": 5, "lines": ["a"], "total": 5})
        hook._push(self.path, seq=4)          # 不是我的活
        self.assertEqual(self.calls, [])
        hook._push(self.path, seq=5)          # 是我的活
        self.assertEqual(self.calls, ["sendRichMessage"])
        self.assertEqual(json.loads(self.path.read_text())["msg_id"], 42)


class ConfigDiagnostics(unittest.TestCase):
    """配置：无代理示例可直接删 proxy 而不留尾逗号；坏 JSON 给脱敏诊断、缺文件静默。"""

    def _cache(self):
        return mcp._file_config.__defaults__[0]

    def test_readme_config_example_valid_without_proxy(self):
        # README 的首个 JSON heredoc 删掉任何 "proxy" 行后仍是合法 JSON（坑：尾逗号）
        readme = (Path(mcp.__file__).parent / "README.md").read_text(encoding="utf-8")
        example = readme.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0]
        without_proxy = "\n".join(l for l in example.splitlines() if '"proxy"' not in l)
        result = json.loads(without_proxy)   # 不合法就抛，测试红
        self.assertIn("bot_token", result)

    def test_malformed_config_emits_redacted_diagnostic(self):
        import io
        from unittest.mock import patch as _p
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text('{"bot_token":"SECRETTOKEN","chat_id":"y",}')  # 尾逗号
            self._cache().clear()
            err = io.StringIO()
            with _p.object(mcp, "CONFIG_PATH", cfg), _p.object(sys, "stderr", err):
                result = mcp._file_config()
            self._cache().clear()
        self.assertEqual(result, {}, "坏配置应按无配置处理，不崩")
        self.assertIn("不是合法 JSON", err.getvalue(), "坏 JSON 该给诊断")
        self.assertNotIn("SECRETTOKEN", err.getvalue(), "诊断不许带出内容/token")

    def test_missing_config_is_silent(self):
        import io
        from unittest.mock import patch as _p
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "nope.json"   # 不存在
            self._cache().clear()
            err = io.StringIO()
            with _p.object(mcp, "CONFIG_PATH", cfg), _p.object(sys, "stderr", err):
                result = mcp._file_config()
            self._cache().clear()
        self.assertEqual(result, {})
        self.assertEqual(err.getvalue(), "", "缺配置文件应静默")


@needs_hook
class EditErrorClassification(unittest.TestCase):
    """B4：进度窗只在**确认消息已不存在**时弃窗；超时/429/5xx 保留窗口。

    误把临时错误当"消息没了"＝清 msg_id、开新窗、旧窗失登记、Stop 收不掉它。

    反向变异：把 _push_locked 里的 `if _looks_message_gone(exc):` 改回无条件
    `_amend_if_gen(...msg_id:0...)`，test_timeout_preserves_window 必转红。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self.tmp.name)
        self.had = os.environ.get("TG_CHAT_ID")
        os.environ["TG_CHAT_ID"] = "888"
        self.original = mcp.call_api

    def tearDown(self):
        mcp.call_api = self.original
        hook.STATE_DIR = self.old_dir
        if self.had is None:
            os.environ.pop("TG_CHAT_ID", None)
        else:
            os.environ["TG_CHAT_ID"] = self.had
        self.tmp.cleanup()

    def _seed(self):
        import time as _t
        path = hook._state_path("s")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seq": 1, "gen": 0, "msg_id": 42,
                                    "lines": ["⚡ Bash"], "total": 1,
                                    "last_push": _t.time()}), encoding="utf-8")
        return path

    def _msg_id(self, path):
        return int(json.loads(path.read_text()).get("msg_id") or 0)

    def test_timeout_preserves_window(self):
        path = self._seed()
        mcp.call_api = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
        hook._push(path, 1)
        self.assertEqual(self._msg_id(path), 42, "临时超时不该弃窗")

    def test_429_preserves_window(self):
        path = self._seed()
        def api(method, data, files=None):
            raise mcp.ApiRejected("Too Many Requests: retry after 5", 429)
        mcp.call_api = api
        hook._push(path, 1)
        self.assertEqual(self._msg_id(path), 42, "429 不该弃窗")

    def test_confirmed_gone_clears_window(self):
        path = self._seed()
        def api(method, data, files=None):
            raise mcp.ApiRejected("Bad Request: message to edit not found", 400)
        mcp.call_api = api
        hook._push(path, 1)
        self.assertEqual(self._msg_id(path), 0, "确认消息没了才该弃窗重开")

    def test_generic_400_preserves_window(self):
        # 400 是杂物袋——不点名消息没了的 400（如参数错）不该弃窗
        path = self._seed()
        def api(method, data, files=None):
            raise mcp.ApiRejected("Bad Request: message text is empty", 400)
        mcp.call_api = api
        hook._push(path, 1)
        self.assertEqual(self._msg_id(path), 42, "无关 400 不该弃窗")


@needs_hook
class ThrottledTailFrame(unittest.TestCase):
    """B6：被节流（没派出子进程）的事件不许让唯一在飞的推送任务失效。

    窗口已存在时 t=1000 的事件派出推送子进程；t=1000.1 又来一次事件，因 1.2s
    节流没派新子进程、只推高了 seq。那个唯一子进程真跑起来时必须照发最新内容，
    不能因为 seq 变了就让位给一个根本不存在的调度任务（结果零编辑）。

    反向变异：把 _superseded 换回 `int(state.get("seq") or 0) != seq`，
    test_only_pending_push_still_edits 必转红（0 次编辑）。
    """

    def setUp(self):
        import time as _t
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self.tmp.name)
        self.had = {k: os.environ.get(k) for k in ("TG_CHAT_ID", "TG_PROGRESS")}
        os.environ["TG_CHAT_ID"] = "888"
        os.environ["TG_PROGRESS"] = "1"
        self.original = mcp.call_api
        path = hook._state_path("s6")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seq": 1, "gen": 0, "msg_id": 42,
                                    "lines": ["⚡ Bash"], "total": 1,
                                    "last_push": 998.0, "draft_id": 9}),
                        encoding="utf-8")
        self.path = path

    def tearDown(self):
        mcp.call_api = self.original
        hook.STATE_DIR = self.old_dir
        for k, v in self.had.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_only_pending_push_still_edits(self):
        import io
        event = json.dumps({"session_id": "s6", "tool_name": "Read",
                            "tool_input": {"file_path": "/tmp/x.py"}})
        from unittest.mock import patch
        with patch.object(hook.subprocess, "Popen") as spawn, \
                patch.object(sys, "argv", ["tg_progress_hook.py"]):
            for stamp in (1000.0, 1000.1):
                with patch.object(hook.time, "time", return_value=stamp), \
                        patch.object(sys, "stdin", io.StringIO(event)):
                    hook.main()
            self.assertEqual(spawn.call_count, 1, "第二帧被节流，不该再派子进程")
            scheduled_seq = int(spawn.call_args.args[0][-1])

        edits = []
        mcp.call_api = lambda *a, **k: (edits.append(a[0]), {"result": {}})[1]
        hook._push(self.path, scheduled_seq)
        self.assertEqual(edits, ["editMessageText"],
                         "唯一在飞的推送被没有子进程的新事件顶掉了，零编辑")


class MediaUpload(unittest.TestCase):
    """media_paths → multipart。坏了＝要么发不出图，要么把凭证文件当图发出去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def img(self, name: str, content: bytes = b"\xff\xd8fake-jpg") -> str:
        p = self.dir / name
        p.write_bytes(content)
        return str(p)

    def test_paths_become_attach_names_in_order(self):
        files = mcp.load_media([self.img("a.jpg"), self.img("b.jpg")])
        self.assertEqual(list(files), ["f0", "f1"])
        self.assertEqual(files["f0"][0], "a.jpg")
        self.assertEqual(files["f1"][1], b"\xff\xd8fake-jpg")

    def test_files_reach_the_api_call(self):
        captured = {}

        def fake_api(method, data, files=None):
            captured.update(method=method, files=files)
            return {"result": {"message_id": 7}}

        original, mcp.call_api = mcp.call_api, fake_api
        os.environ["TG_CHAT_ID"] = "42"
        try:
            reply = call_tool("tg_rich_send", {
                "blocks": [{"type": "photo",
                            "photo": {"type": "photo", "media": "attach://f0"}}],
                "media_paths": [self.img("cat.jpg")],
            })
        finally:
            mcp.call_api = original
            os.environ.pop("TG_CHAT_ID", None)
        self.assertFalse(reply["result"].get("isError"))
        self.assertEqual(captured["method"], "sendRichMessage")
        self.assertIn("f0", captured["files"])

    def test_media_requires_blocks(self):
        with_markdown = call_tool("tg_rich_send", {
            "markdown": "hi", "media_paths": [self.img("cat.jpg")]})
        self.assertTrue(with_markdown["result"]["isError"])
        self.assertIn("blocks", with_markdown["result"]["content"][0]["text"])

    def test_missing_file_rejected(self):
        with self.assertRaises(ValueError):
            mcp.load_media([str(self.dir / "nope.jpg")])

    def test_too_many_rejected(self):
        paths = [self.img(f"p{i}.jpg") for i in range(3)]
        original, mcp.MEDIA_MAX_COUNT = mcp.MEDIA_MAX_COUNT, 2
        try:
            with self.assertRaises(ValueError):
                mcp.load_media(paths)
        finally:
            mcp.MEDIA_MAX_COUNT = original

    def test_oversize_rejected(self):
        original, mcp.MEDIA_MAX_BYTES = mcp.MEDIA_MAX_BYTES, 4
        try:
            with self.assertRaises(ValueError) as ctx:
                mcp.load_media([self.img("big.jpg", b"12345")])
        finally:
            mcp.MEDIA_MAX_BYTES = original
        self.assertIn("50MB", str(ctx.exception))

    def test_credential_shaped_names_blocked(self):
        for name in [".env", ".env.production", "id_rsa", "server.pem",
                     "my_token.png", "aws_credentials.jpg"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    mcp.load_media([self.img(name)])
                self.assertIn("凭证", str(ctx.exception))

    def test_symlink_checked_by_real_target(self):
        """链接名无害不代表指向的东西无害。"""
        secret = Path(self.img("server.pem"))
        link = self.dir / "innocent.jpg"
        link.symlink_to(secret)
        with self.assertRaises(ValueError):
            mcp.load_media([str(link)])

    def test_guard_can_be_disabled(self):
        """闸必须能关，而且关了要真的放行——这一条同时当变异测试：
        证明上面的拦截真是闸干的，不是别处碰巧报错。"""
        os.environ["TG_RICH_MEDIA_GUARD"] = "0"
        try:
            files = mcp.load_media([self.img(".env")])
        finally:
            os.environ.pop("TG_RICH_MEDIA_GUARD", None)
        self.assertEqual(list(files), ["f0"])

    def test_ordinary_photo_names_survive(self):
        for name in ["IMG_20260730.jpg", "猫猫.png", "screenshot-1.webp"]:
            with self.subTest(name=name):
                self.assertEqual(list(mcp.load_media([self.img(name)])), ["f0"])

    def test_file_id_extraction_prefers_biggest_variant(self):
        result = {"rich_message": {"blocks": [
            {"type": "collage", "blocks": [
                {"photo": [
                    {"file_id": "small", "width": 41, "height": 90},
                    {"file_id": "big", "width": 581, "height": 1280},
                ]},
            ]},
        ]}}
        self.assertEqual(mcp.extract_file_ids(result), ["big"])

    def test_total_budget_rejects_legal_singles_that_sum_over(self):
        """澄审 P1-2 点名的反例：每个文件都合法，加在一起超总预算——必须拒。

        不设总量闸的话 50 × 50MB ＝ 一次调用先吃 2.5GB RSS。
        预算缩到 1MB 来测（600KB × 2 单个合法、合计超），断言盯「总预算」
        字样——单文件闸误拦的话报的是 50MB 上限，这条会红。
        """
        os.environ["TG_RICH_MEDIA_TOTAL_MB"] = "1"
        try:
            a = self.img("a.jpg", b"x" * (600 * 1024))
            b = self.img("b.jpg", b"y" * (600 * 1024))
            with self.assertRaises(ValueError) as ctx:
                mcp.load_media([a, b])
            self.assertIn("总预算", str(ctx.exception))
            # 单发每一个都必须还过得去——证明拦的真是「累计」不是「单个」
            self.assertEqual(list(mcp.load_media([a])), ["f0"])
            self.assertEqual(list(mcp.load_media([b])), ["f0"])
        finally:
            os.environ.pop("TG_RICH_MEDIA_TOTAL_MB", None)

    def test_total_budget_default_leaves_normal_batches_alone(self):
        # 默认 200MB：正常九宫格（9 × 小图）一根汗毛都不该动
        batch = [self.img(f"p{i}.jpg") for i in range(9)]
        self.assertEqual(len(mcp.load_media(batch)), 9)

    def test_total_budget_bad_env_falls_back(self):
        os.environ["TG_RICH_MEDIA_TOTAL_MB"] = "胡写的"
        try:
            self.assertEqual(mcp._media_total_budget(),
                             mcp.MEDIA_MAX_TOTAL_MB_DEFAULT * 1024 * 1024)
        finally:
            os.environ.pop("TG_RICH_MEDIA_TOTAL_MB", None)

    def test_oversize_file_is_not_read_into_memory(self):
        """B3：超预算的文件按 stat 预检直接拒，一个字节都不读进内存。

        反向变异：把 load_media 的 stat 预检去掉、先 read_bytes 再判预算，
        本测试转红（read_bytes 被调用、reads 非空）。
        """
        from unittest.mock import patch as _p
        big = self.img("big.jpg", b"x" * (2 * 1024 * 1024))   # 2MB
        reads = []
        real_rb = Path.read_bytes

        def spy(path):
            reads.append(str(path))
            return real_rb(path)

        os.environ["TG_RICH_MEDIA_TOTAL_MB"] = "1"
        try:
            with _p.object(Path, "read_bytes", spy):
                with self.assertRaises(ValueError) as ctx:
                    mcp.load_media([big])
        finally:
            os.environ.pop("TG_RICH_MEDIA_TOTAL_MB", None)
        self.assertEqual(reads, [], "超预算的文件不该被读进内存")
        self.assertIn("总预算", str(ctx.exception))

    def test_growth_during_read_still_rejected(self):
        """B3：stat 骗过闸、read 时文件长大——按实际字节再验单文件上限，仍拒。

        反向变异：去掉读后的 `len(blob) > cap` 复验，本测试转红（32 字节被接收）。
        """
        from unittest.mock import patch as _p
        photo = self.dir / "g.webp"
        photo.write_bytes(b"x")
        real_rb = Path.read_bytes

        def growing(path):
            # 按文件名命中，不用 == 全等：实现读的是 resolve() 后的路径，
            # Windows 上 temp 目录经 resolve 会展开 8.3 短路径/统一盘符大小写，
            # 与未 resolve 的 photo 不相等，钩子落空 → 文件没长大 → 测试假绿。
            if path.name == photo.name:
                photo.write_bytes(b"x" * 32)   # stat 时 1 字节，读之前长到 32
            return real_rb(path)

        with _p.object(mcp, "MEDIA_MAX_BYTES", 8), \
                _p.object(Path, "read_bytes", growing):
            with self.assertRaises(ValueError):
                mcp.load_media([str(photo)])


@needs_hook
class ConcurrentStateWrites(unittest.TestCase):
    """并行工具调用会同时写状态文件——无锁时后写的覆盖先写的。"""

    def test_no_lines_lost_under_concurrency(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, TG_PROGRESS_REDACT="1")
            env.pop("TG_BOT_TOKEN", None)     # 确保推送子进程发不出去
            env.pop("TG_CHAT_ID", None)
            procs = []
            for i in range(60):
                event = json.dumps({"tool_name": "Read", "session_id": "concurrency",
                                    "tool_input": {"file_path": f"/x/file{i}.py"}})
                procs.append(subprocess.Popen(
                    [sys.executable, str(HERE / "tg_progress_hook.py")],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env, text=True))
                procs[-1].stdin.write(event)
                procs[-1].stdin.close()
            for p in procs:
                p.wait(timeout=30)

            state_dir = Path(home) / ".tg-progress"
            states = list(state_dir.glob("*.json"))
            self.assertEqual(len(states), 1, "一个 session 应该只有一个状态文件")
            state = json.loads(states[0].read_text(encoding="utf-8"))
            self.assertEqual(state["total"], 60, "有帧被覆盖丢掉了")
            self.assertLessEqual(len(state["lines"]), 40, "lines 应该截断在 40")


class OrphanStickerGuard(unittest.TestCase):
    """孤儿贴纸防护：脸不许先于它所依附的那句话出门。

    位置即语义——脸是贴给它前面那句话的。贴纸先送达、正文随后失败＝对方收到
    一张没头没尾的脸，比缺一张脸更糟。这组测试拆掉防护必须转红：
    `test_leading_sticker_waits_for_text`（顺序翻转）和
    `test_text_failure_never_sends_the_face`（孤儿脸照发）就是那两支反向探针。
    """

    def setUp(self):
        from test_tg_sticker import make_library
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TG_STICKER_DIR"] = self.tmp.name
        make_library(Path(self.tmp.name))
        self.had = {k: os.environ.get(k) for k in ("TG_CHAT_ID", "TG_BOT_TOKEN")}
        os.environ["TG_CHAT_ID"] = "-100555"
        os.environ["TG_BOT_TOKEN"] = FAKE_TOKEN
        self.calls: list[tuple[str, dict]] = []
        self.fail_on: str | None = None
        self.original = mcp.call_api

        def fake(method, data, files=None):
            if method == self.fail_on:
                raise RuntimeError("模拟：这条 API 炸了")
            self.calls.append((method, dict(data)))
            return {"ok": True, "result": {
                "message_id": len(self.calls),
                "sticker": {"file_id": "FRESH", "file_unique_id": "UNIQ0"},
            }}

        mcp.call_api = fake
        mcp._LAST_SENT.clear()

    def tearDown(self):
        mcp.call_api = self.original
        os.environ.pop("TG_STICKER_DIR", None)
        for k, v in self.had.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mcp._LAST_SENT.clear()
        self.tmp.cleanup()

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def test_leading_sticker_waits_for_text(self):
        # 标记在句首：贴纸段排在文字段前面，但发送顺序必须是话先出门
        out = mcp.tool_send({"markdown": "（😭）我错了"})
        self.assertEqual(self.methods(), ["sendRichMessage", "sendSticker"])
        self.assertIn("贴纸「大哭猫」", out)

    def test_text_failure_never_sends_the_face(self):
        # 正文炸了 ⇒ 挂起的脸永不发送——宁可对方什么都没收到
        self.fail_on = "sendRichMessage"
        with self.assertRaises(RuntimeError):
            mcp.tool_send({"markdown": "（😭）我错了"})
        self.assertNotIn("sendSticker", self.methods())

    def test_pure_sticker_message_goes_straight_out(self):
        # 没有正文段＝没有"所依附的那句话"，不存在孤儿问题，照常直发
        mcp.tool_send({"markdown": "（😭）"})
        self.assertEqual(self.methods(), ["sendSticker"])

    def test_sticker_between_texts_keeps_position(self):
        # 前面已有正文送达的贴纸不受防护影响，位置即语义原样保住
        mcp.tool_send({"markdown": "第一句（😭）第二句"})
        self.assertEqual(self.methods(),
                         ["sendRichMessage", "sendSticker", "sendRichMessage"])

    def test_sticker_failure_still_spares_the_text(self):
        # 反过来不变：脸发失败不牵连正文（坑 17 的老纪律）。
        # R2：fake 的 sendSticker 抛的是**普通 RuntimeError（网络式）**，按文字/贴纸
        # 一套口径应归 unknown（可能已送达、禁止自动补发），不是 failed；无论哪种，
        # 两条正文都照常送出去（脸的成败不牵连话）。
        self.fail_on = "sendSticker"
        out = mcp.tool_send({"markdown": "第一句（😭）第二句"})
        self.assertEqual(self.methods().count("sendRichMessage"), 2)
        self.assertIn("贴纸送达状态未知", out)
        ledger = json.loads(out.split("分段送达账（机器可读）：", 1)[1])
        self.assertTrue(any(x["kind"] == "sticker" for x in ledger["unknown"]))
        self.assertEqual(ledger["failed"], [], "网络式错误不该记成服务器拒收")

    def test_reply_to_lands_on_first_delivered_text(self):
        # 句首贴纸被挂起后，引用回复应该落在第一条真送出去的正文上
        mcp.tool_send({"markdown": "（😭）我错了", "reply_to": "42"})
        method, data = self.calls[0]
        self.assertEqual(method, "sendRichMessage")
        self.assertIn('"message_id": 42', data.get("reply_parameters", ""))


class MarkerPathContract(unittest.TestCase):
    """B7：命中贴纸标记的分支不许改变参数合同——先公共校验，silent/rtl 共享。

    反向变异：把 tool_send 里 build_rich 移回贴纸分支之后 → 校验前置失效，
    test_invalid_combo_rejected_before_send 转红；去掉 send_entry 的 silent 透传
    或分段正文的 is_rtl → 对应测试转红。
    """

    def setUp(self):
        from test_tg_sticker import make_library
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TG_STICKER_DIR"] = self.tmp.name
        make_library(Path(self.tmp.name))
        self.had = {k: os.environ.get(k) for k in ("TG_CHAT_ID", "TG_BOT_TOKEN")}
        os.environ["TG_CHAT_ID"] = "-100555"
        os.environ["TG_BOT_TOKEN"] = FAKE_TOKEN
        self.calls = []
        self.original = mcp.call_api

        def fake(method, data, files=None):
            self.calls.append((method, dict(data)))
            return {"ok": True, "result": {
                "message_id": len(self.calls),
                "sticker": {"file_id": "FRESH", "file_unique_id": "UNIQ2"}}}

        mcp.call_api = fake
        mcp._LAST_SENT.clear()

    def tearDown(self):
        mcp.call_api = self.original
        os.environ.pop("TG_STICKER_DIR", None)
        for k, v in self.had.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mcp._LAST_SENT.clear()
        self.tmp.cleanup()

    def test_invalid_combo_rejected_before_send(self):
        # markdown+html 命中贴纸标记：本该三选一报错，绝不因命中贴纸而放行发送
        result = call_tool("tg_rich_send", {"markdown": "hi（😭）", "html": "<b>x</b>"})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("三选一", result["result"]["content"][0]["text"])
        self.assertEqual(self.calls, [], "非法请求不该有任何发送副作用")

    def test_silent_reaches_stickers(self):
        mcp.tool_send({"markdown": "hi（😭）", "silent": True})
        sticker_calls = [d for m, d in self.calls if m == "sendSticker"]
        self.assertTrue(sticker_calls, "贴纸没发出去")
        self.assertEqual(sticker_calls[0].get("disable_notification"), "true")
        # 文字段也应静默
        text_calls = [d for m, d in self.calls if m == "sendRichMessage"]
        self.assertEqual(text_calls[0].get("disable_notification"), "true")

    def test_rtl_reaches_segmented_text(self):
        mcp.tool_send({"markdown": "hi（😭）", "rtl": True})
        text_calls = [d for m, d in self.calls if m == "sendRichMessage"]
        rich = json.loads(text_calls[0]["rich_message"])
        self.assertTrue(rich.get("is_rtl"))


class PartialSendLedger(unittest.TestCase):
    """B2：分段发送后段失败，前段已送达的 message_id 绝不能从错误里丢。

    坑 17 的记账要覆盖**后续文字段失败**，不只贴纸段：返回机器可读的
    delivered/failed/unknown + 已确认 id；超时段标 unknown（可能已送达，禁止
    自动整条重发），服务器拒收标 failed（肯定没发，可安全补这一段）。

    反向变异：把 _send_with_stickers 里 text 发送的 try/except 去掉（让异常直接
    冒出去），test_later_text_failure_keeps_delivered_id 必转红（701 从结果消失）。
    """

    def setUp(self):
        from test_tg_sticker import make_library
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TG_STICKER_DIR"] = self.tmp.name
        make_library(Path(self.tmp.name))
        self.had = {k: os.environ.get(k) for k in ("TG_CHAT_ID", "TG_BOT_TOKEN")}
        os.environ["TG_CHAT_ID"] = "-100555"
        os.environ["TG_BOT_TOKEN"] = FAKE_TOKEN
        # 本 bot 先缓存好 file_id，贴纸段直接走缓存、不碰归档上传
        import tg_sticker
        tg_sticker.remember_file_id(FAKE_TOKEN, "UNIQ2", "CACHED_FID")
        self.texts: list[str] = []
        self.original = mcp.call_api
        mcp._LAST_SENT.clear()

    def tearDown(self):
        mcp.call_api = self.original
        os.environ.pop("TG_STICKER_DIR", None)
        for k, v in self.had.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mcp._LAST_SENT.clear()
        self.tmp.cleanup()

    def _ledger(self, text: str) -> dict:
        marker = "分段送达账（机器可读）："
        return json.loads(text.split(marker, 1)[1])

    def test_later_text_failure_keeps_delivered_id(self):
        # first 段送达（id 701）→ 贴纸 →  second 段超时。已送达的 701 必须还在结果里。
        def api(method, data, files=None):
            if method == "sendRichMessage":
                self.texts.append(json.loads(data["rich_message"])["markdown"])
                if len(self.texts) == 2:
                    raise RuntimeError("simulated timeout")
                return {"ok": True, "result": {"message_id": 701}}
            return {"ok": True, "result": {"message_id": 999}}

        mcp.call_api = api
        result = call_tool("tg_rich_send", {"markdown": "first（😭）second"})
        self.assertEqual(self.texts, ["first", "second"])
        blob = json.dumps(result, ensure_ascii=False)
        self.assertIn("701", blob, "已送达的 message_id 从错误结果里丢了")
        self.assertTrue(result["result"].get("isError"))
        ledger = self._ledger(result["result"]["content"][0]["text"])
        self.assertIn(701, [d.get("message_id") for d in ledger["delivered"]])
        self.assertTrue(ledger["unknown"], "超时段应记进 unknown（可能已送达）")
        self.assertEqual(ledger["unknown"][0]["kind"], "text")

    def test_api_rejection_marks_failed_not_unknown(self):
        # 服务器明确拒收（ok:false → ApiRejected）＝这段肯定没发出去，进 failed 不进 unknown
        def api(method, data, files=None):
            if method == "sendRichMessage":
                self.texts.append(json.loads(data["rich_message"])["markdown"])
                if len(self.texts) == 2:
                    raise mcp.ApiRejected("API 拒收: message is too long", 400)
                return {"ok": True, "result": {"message_id": 701}}
            return {"ok": True, "result": {"message_id": 999}}

        mcp.call_api = api
        result = call_tool("tg_rich_send", {"markdown": "first（😭）second"})
        ledger = self._ledger(result["result"]["content"][0]["text"])
        self.assertIn(701, [d.get("message_id") for d in ledger["delivered"]])
        self.assertEqual(ledger["unknown"], [], "拒收不该记成送达状态未知")
        self.assertTrue(any(f.get("kind") == "text" for f in ledger["failed"]))

    def test_success_carries_machine_readable_ledger(self):
        def api(method, data, files=None):
            return {"ok": True, "result": {"message_id": 55}}

        mcp.call_api = api
        out = mcp.tool_send({"markdown": "第一句（😭）第二句"})
        ledger = self._ledger(out)
        kinds = [d["kind"] for d in ledger["delivered"]]
        self.assertEqual(kinds, ["text", "sticker", "text"])
        self.assertEqual(ledger["failed"], [])
        self.assertEqual(ledger["unknown"], [])

    def test_sticker_timeout_is_unknown_not_safe_to_retry(self):
        # R2（收编审查反例）：sendSticker 抛网络式 RuntimeError（请求可能已到）＝
        # 送达状态未知，进 unknown，绝不进 failed（failed 会被调用方当「肯定没发」
        # 安全补发 → 重复贴纸）。
        def api(method, data, files=None):
            if method == "sendSticker":
                raise RuntimeError("lost response after sending request")
            return {"ok": True, "result": {"message_id": 701}}

        mcp.call_api = api
        ledger = self._ledger(mcp.tool_send({"markdown": "hello（😭）"}))
        self.assertTrue(any(x["kind"] == "sticker" for x in ledger["unknown"]), ledger)
        self.assertFalse(any(x["kind"] == "sticker" for x in ledger["failed"]), ledger)

    def test_successful_sticker_with_failed_cache_is_delivered(self):
        # R2（收编审查反例）：sendSticker 已返回成功，随后 remember_file_id 因磁盘满
        # 失败——贴纸已经送达，必须记 delivered + 缓存告警，绝不误报成 failed。
        import tg_sticker
        # 归档原图要在（走归档重传路径），且缓存为空以强制走上传分支
        img = Path(self.tmp.name) / "img" / "002.webp"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"fake-image")
        actual_delivery = []

        def api(method, data, files=None):
            if method == "sendSticker":
                actual_delivery.append(778)
                return {"ok": True, "result": {"message_id": 778,
                                               "sticker": {"file_id": "FRESH"}}}
            return {"ok": True, "result": {"message_id": 701}}

        mcp.call_api = api
        with mock.patch.object(tg_sticker, "load_cache", return_value={}), \
                mock.patch.object(tg_sticker, "remember_file_id",
                                  side_effect=OSError("disk full")):
            ledger = self._ledger(mcp.tool_send({"markdown": "hello（😭）"}))
        self.assertEqual(actual_delivery, [778])
        stickers_delivered = [x for x in ledger["delivered"] if x["kind"] == "sticker"]
        self.assertTrue(stickers_delivered, ledger)
        self.assertEqual(stickers_delivered[0].get("message_id"), 778)
        self.assertTrue(stickers_delivered[0].get("cache_warning"),
                        "缓存写失败应留告警，但送达仍成立")

    def test_all_planned_segments_accounted_for_after_abort(self):
        # R4（收编审查反例）：first（😺）second（😺）third＝5 段；第 2 段正文被拒收，
        # 尾部 4、5 段从没试过发——必须进 not_attempted，五段全都有归属，不许凭空消失。
        count = 0

        def api(method, data, files=None):
            nonlocal count
            if method == "sendRichMessage":
                count += 1
                if count == 2:
                    raise mcp.ApiRejected("unsupported format", 400)
            return {"ok": True, "result": {"message_id": 701}}

        mcp.call_api = api
        result = call_tool("tg_rich_send", {"markdown": "first（😭）second（😭）third"})
        ledger = self._ledger(result["result"]["content"][0]["text"])
        actual = {item["seg"] for values in ledger.values()
                  for item in values if isinstance(item, dict)}
        self.assertEqual(actual, {1, 2, 3, 4, 5}, f"计划段有缺口：{ledger}")
        self.assertTrue(ledger["not_attempted"], "未遍历的尾段应进 not_attempted")
        self.assertEqual({x["seg"] for x in ledger["not_attempted"]}, {4, 5})


class DraftCanStop(unittest.TestCase):
    """can_stop / keep_on_stop 透传：开了才带，没开一个字节都不多发。"""

    def setUp(self):
        self.calls = []
        self.original = mcp.call_api
        mcp.call_api = lambda method, data, files=None: (
            self.calls.append((method, dict(data))), {"result": {}})[1]
        self.had = os.environ.get("TG_CHAT_ID")
        os.environ["TG_CHAT_ID"] = "888"
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TG_STICKER_DIR"] = self.tmp.name   # 空库＝标记层不掺和

    def tearDown(self):
        mcp.call_api = self.original
        if self.had is None:
            os.environ.pop("TG_CHAT_ID", None)
        else:
            os.environ["TG_CHAT_ID"] = self.had
        os.environ.pop("TG_STICKER_DIR", None)
        self.tmp.cleanup()

    def test_default_sends_neither(self):
        mcp.tool_draft({"markdown": "x", "draft_id": 1})
        _, data = self.calls[0]
        self.assertNotIn("can_stop", data)
        self.assertNotIn("keep_on_stop", data)

    def test_can_stop_passes_through(self):
        mcp.tool_draft({"markdown": "x", "draft_id": 1, "can_stop": True})
        _, data = self.calls[0]
        self.assertEqual(data.get("can_stop"), "true")
        self.assertNotIn("keep_on_stop", data)

    def test_keep_on_stop_requires_can_stop(self):
        # 单给 keep_on_stop 不带 can_stop ＝ 没有按钮可按，参数不该出门
        mcp.tool_draft({"markdown": "x", "draft_id": 1, "keep_on_stop": True})
        _, data = self.calls[0]
        self.assertNotIn("keep_on_stop", data)

    def test_both_pass_through(self):
        mcp.tool_draft({"markdown": "x", "draft_id": 1,
                        "can_stop": True, "keep_on_stop": True})
        _, data = self.calls[0]
        self.assertEqual(data.get("can_stop"), "true")
        self.assertEqual(data.get("keep_on_stop"), "true")


@needs_hook
class StopVsInflightPushFence(unittest.TestCase):
    """Stop ↔ 在途推送的竞态（澄 2026-09-04 审出）。

    病灶：push 的 sendRichMessage 还在飞，_finish 先读到 msg_id=0 清了账退场，
    随后 push 返回把新 msg_id 写回来——任务结束后反而冒出一扇孤儿进度窗。
    修法是代际栅栏（gen fence）：finish 关账时 gen+1；晚归的 push CAS(gen) 失败，
    不登记、并把自己刚发出去的消息删掉。**普通锁挡不住这个**——锁管「别同时写」，
    管不了「这一轮已经死了别再写」。

    反向变异：把 _push_locked 里的 _amend_if_gen 换回 _amend（或把 finish 的
    gen+1 拿掉），test_finish_beats_inflight_push_no_orphan 两条断言都必须转红。
    """

    def setUp(self):
        import threading
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self.tmp.name)
        self.had = {k: os.environ.get(k) for k in
                    ("TG_CHAT_ID", "TG_PROGRESS_MODE", "TG_PROGRESS_END")}
        os.environ["TG_CHAT_ID"] = "888"
        os.environ.pop("TG_PROGRESS_MODE", None)   # edit 模式
        os.environ.pop("TG_PROGRESS_END", None)    # delete 收场
        self.original = mcp.call_api
        self.send_started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, dict]] = []

        def fake(method, data, files=None):
            if method == "sendRichMessage":
                self.send_started.set()
                self.release.wait(5)               # 卡住请求，模拟网络在飞
            self.calls.append((method, dict(data)))
            return {"result": {"message_id": 77}}

        mcp.call_api = fake

    def tearDown(self):
        mcp.call_api = self.original
        hook.STATE_DIR = self.old_dir
        for k, v in self.had.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _seed(self, **extra) -> Path:
        import time as _t
        state = {"seq": 1, "gen": 0, "claim": 1, "claim_at": _t.time(),
                 "lines": ["⚡ Bash"], "total": 1, "last_push": _t.time(),
                 "draft_id": 9}
        state.update(extra)
        path = hook._state_path("s1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def _deleted(self) -> list[int]:
        return [int(d.get("message_id") or 0)
                for m, d in self.calls if m == "deleteMessage"]

    def test_finish_beats_inflight_push_no_orphan(self):
        # 澄点名的那支：卡住 sendRichMessage → 触发 finish → 放行请求
        import threading
        path = self._seed()
        worker = threading.Thread(target=hook._push, args=(path, 1))
        worker.start()
        self.assertTrue(self.send_started.wait(5), "send 没起飞，测试环境不对")
        hook._finish_session("s1")                 # 请求还在飞时收窗
        self.release.set()
        worker.join(5)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(int(state.get("msg_id") or 0), 0,
                         "孤儿窗被重新登记——栅栏失效")
        self.assertIn(77, self._deleted(),
                      "晚归的消息没有自我了断——聊天里会留一扇孤儿窗")

    def test_normal_finish_deletes_registered_window(self):
        # 无并发的老路径不许被栅栏改坏：已登记的窗收工照删
        path = self._seed(msg_id=42)
        self.release.set()
        hook._finish_session("s1")
        self.assertEqual(self._deleted(), [42])
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(int(state.get("msg_id") or 0), 0)
        self.assertEqual(int(state.get("gen") or 0), 1)

    def test_amend_if_gen_refuses_stale_writer(self):
        path = self._seed(gen=5)
        self.assertFalse(hook._amend_if_gen(path, 4, {"msg_id": 99}))
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("msg_id", state)
        self.assertTrue(hook._amend_if_gen(path, 5, {"msg_id": 99}))
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["msg_id"], 99)

    def test_stale_edit_failure_cannot_wipe_new_round(self):
        # 编辑失败的让位补丁也要过栅栏：旧轮的「松手」不许清掉新一轮的窗。
        # 关键在时序——push 必须先读走旧账（gen=0）并卡在编辑上，等新一轮
        # 开出来（gen=1、开了 55 号窗）后才失败让位。
        # 失败必须是**确认消息没了**（B4 之后只有这类才触发清窗）；用 _amend 直接
        # 模拟新一轮（不走 _finish_session，避开它把清理排到推送锁尾的等待）。
        import threading
        path = self._seed(msg_id=42)          # 旧轮：有窗可编辑
        edit_started = threading.Event()
        release_edit = threading.Event()

        def flaky(method, data, files=None):
            self.calls.append((method, dict(data)))
            if method == "editMessageText":
                edit_started.set()
                release_edit.wait(5)
                raise mcp.ApiRejected("Bad Request: message to edit not found", 400)
            return {"result": {"message_id": 77}}

        mcp.call_api = flaky
        worker = threading.Thread(target=hook._push, args=(path, 1))
        worker.start()
        self.assertTrue(edit_started.wait(5), "edit 没起飞，测试环境不对")
        # 新一轮：bump gen + 开 55 号窗（_amend 走 state 锁，不与 push 持的推送锁抢）
        hook._amend(path, {"gen": 1, "msg_id": 55, "claim": 7, "claim_at": 1.0})
        release_edit.set()
        worker.join(5)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(int(state.get("msg_id") or 0), 55,
                         "旧轮 push 把新一轮的窗清掉了")


@needs_hook
class StopLifecycleAcrossModes(unittest.TestCase):
    """B5：Stop 的代际保护要覆盖 keep 的晚归编辑与 draft 的排队推送。

    - keep：Stop 的"干完了"最终编辑与在飞的旧帧共用推送锁，必须排在旧帧之后，
      否则旧帧晚归把窗口改回"正在干活…"。
    - draft：Stop 清空并 bump seq/gen 后，排队的旧 draft 子进程必须让位，
      不能再喷一帧"正在干活/0步"。

    反向变异：keep — 去掉 _finish_session 第二步的推送锁包裹，
    test_keep_final_edit_lands_after_inflight 转红；
    draft — 把 _finish_session 第一步的 seq/sched_seq bump 拿掉，
    test_queued_draft_yields_after_finish 转红。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self.tmp.name)
        self.had = {k: os.environ.get(k) for k in
                    ("TG_CHAT_ID", "TG_PROGRESS_MODE", "TG_PROGRESS_END")}
        os.environ["TG_CHAT_ID"] = "888"
        self.original = mcp.call_api

    def tearDown(self):
        mcp.call_api = self.original
        hook.STATE_DIR = self.old_dir
        for k, v in self.had.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _seed(self, **extra):
        import time as _t
        state = {"seq": 1, "gen": 0, "claim": 1, "claim_at": _t.time(),
                 "msg_id": 42, "lines": ["⚡ Bash"], "total": 1,
                 "last_push": _t.time(), "draft_id": 9}
        state.update(extra)
        path = hook._state_path("s5")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_keep_final_edit_lands_after_inflight(self):
        import threading
        os.environ["TG_PROGRESS_MODE"] = "edit"
        os.environ["TG_PROGRESS_END"] = "keep"
        path = self._seed()
        started, hold = threading.Event(), threading.Event()
        applied = []

        def api(method, data, files=None):
            if method == "editMessageText":
                heading = json.loads(data["rich_message"])["blocks"][0]["text"]
                if heading == hook.TITLE:
                    started.set()
                    hold.wait(3)            # 旧帧卡在编辑里、握着推送锁
                applied.append(heading)
            return {"ok": True, "result": {}}

        mcp.call_api = api
        push = threading.Thread(target=hook._push, args=(path, 1))
        push.start()
        self.assertTrue(started.wait(3), "旧帧没起飞")
        # finish 放到线程里，好让主线程放行旧帧——不把 release 绑在 finish 返回上，
        # 于是不依赖被审提交的内部时序（是稳定并发测试，不是屏障钩子照搬）。
        finisher = threading.Thread(target=hook._finish_session, args=("s5",))
        finisher.start()
        import time as _t
        _t.sleep(0.05)                      # 让 finish 先卡在推送锁上
        hold.set()                          # 放行旧帧：发 TITLE、放锁
        push.join(3)
        finisher.join(3)
        self.assertEqual(applied, [hook.TITLE, hook.DONE_TITLE],
                         f"最终编辑没排到在飞旧帧之后：{applied}")

    def test_queued_draft_yields_after_finish(self):
        os.environ["TG_PROGRESS_MODE"] = "draft"
        path = self._seed(msg_id=0)
        calls = []
        mcp.call_api = lambda *a, **k: (calls.append(a[0]), {"result": True})[1]
        hook._finish_session("s5")
        hook._push(path, 1)                 # 排队的旧 draft 子进程
        self.assertEqual(calls, [],
                         "Stop 后排队的旧 draft 还喷了一帧空草稿")


if __name__ == "__main__":
    unittest.main(verbosity=2)
