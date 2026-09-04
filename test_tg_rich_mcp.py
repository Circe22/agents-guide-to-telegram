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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import secret_redaction  # noqa: E402
import tg_progress_hook as hook  # noqa: E402
import tg_rich_mcp as mcp  # noqa: E402

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

    def test_fallback_copy_matches_source_of_truth(self):
        """盯的必须是**副本的字面量**。

        比 `hook._TELEGRAM_TOKEN_RE.pattern` 是没用的：测试环境下 import 一定成功，
        那个名字就是共享对象本身，等于断言 A == A——副本改回 `\\b\\d{8,}` 也照绿。
        """
        self.assertEqual(hook._FALLBACK_TELEGRAM_TOKEN_PATTERN,
                         secret_redaction.TELEGRAM_BOT_TOKEN_RE.pattern)

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
        # 反过来不变：脸发失败不牵连正文（坑 17 的老纪律，v4 一个字没动）
        self.fail_on = "sendSticker"
        out = mcp.tool_send({"markdown": "第一句（😭）第二句"})
        self.assertEqual(self.methods().count("sendRichMessage"), 2)
        self.assertIn("贴纸未送达", out)

    def test_reply_to_lands_on_first_delivered_text(self):
        # 句首贴纸被挂起后，引用回复应该落在第一条真送出去的正文上
        mcp.tool_send({"markdown": "（😭）我错了", "reply_to": "42"})
        method, data = self.calls[0]
        self.assertEqual(method, "sendRichMessage")
        self.assertIn('"message_id": 42', data.get("reply_parameters", ""))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
