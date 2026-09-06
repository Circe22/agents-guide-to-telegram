#!/usr/bin/env python3
"""tg_ask（按钮选择题）的测试。

跑：
    python3 -m unittest test_tg_ask -v

**一条网络请求都不发**——call_api 全程是录像机假件，按剧本回放 getUpdates。

测的是那些"坏了会伤到别人"的地方：
  · callback_data 字节预算（坏了＝Telegram 400，题发不出去）
  · owner 校验（坏了＝别人替她做决定）
  · 幽灵按钮与积压清理（坏了＝上一题的点击混进这一题）
  · 409 文案（坏了＝用户对着傻等的工具抓瞎）
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tg_ask  # noqa: E402
import tg_rich_mcp as mcp  # noqa: E402
from tg_sticker import ApiRejected  # noqa: E402


class FakeApi:
    """录像机 + 剧本回放。

    plan 是 getUpdates 的批次剧本：每个元素是 list（直接回放）或
    callable（拿着 self 现场造——发送之后才知道 nonce，得懒生成）。
    剧本耗尽后回空批次（等价于长轮询空转）。
    """

    def __init__(self, plan=None, message_id=111):
        self.calls: list[tuple[str, dict]] = []
        self.plan = list(plan or [])
        self.message_id = message_id
        self._update_id = 9000
        self._cq = 0

    # -- 剧本用的小工具 --
    def sent_keyboard(self) -> list[dict]:
        """最近一次 sendMessage 的按钮平铺（行展开）。"""
        for method, data in reversed(self.calls):
            if method == "sendMessage":
                markup = json.loads(data["reply_markup"])
                return [b for row in markup["inline_keyboard"] for b in row]
        raise AssertionError("还没 sendMessage 过")

    def sent_text(self) -> str:
        for method, data in reversed(self.calls):
            if method == "sendMessage":
                return data["text"]
        raise AssertionError("还没 sendMessage 过")

    def click(self, index: int, from_id: str = "8888", data: str | None = None):
        """造一条 callback_query update。data 显式给了就不看键盘。"""
        self._update_id += 1
        self._cq += 1
        payload = data if data is not None else (
            self.sent_keyboard()[index]["callback_data"]
        )
        return {
            "update_id": self._update_id,
            "callback_query": {
                "id": f"cq{self._cq}",
                "from": {"id": int(from_id)},
                "data": payload,
            },
        }

    # -- call_api 本体 --
    def __call__(self, method, data, files=None):
        self.calls.append((method, dict(data)))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": self.message_id}}
        if method == "getUpdates":
            batch = self.plan.pop(0) if self.plan else []
            if callable(batch):
                batch = batch(self)
            return {"ok": True, "result": batch}
        return {"ok": True, "result": {}}

    def named(self, method):
        return [(m, d) for m, d in self.calls if m == method]


def ask(api, plan_done=None, **over):
    args = {"question": "选哪个？", "options": ["A", "B", "C"], "timeout_s": 5}
    args.update(over)
    return json.loads(tg_ask.tool_ask_choice(args, over.get("chat", "8888"), api))


class WidthAndLayout(unittest.TestCase):
    """16 字线——真机实测的那条剪刀线。"""

    def test_cjk_full_latin_half(self):
        self.assertEqual(tg_ask.width("中文"), 2.0)
        self.assertEqual(tg_ask.width("abcd"), 2.0)

    def test_sixteen_cjk_still_buttons(self):
        self.assertEqual(tg_ask.pick_layout(["甲" * 16]), "buttons")

    def test_one_long_flips_whole_question(self):
        """一条超线，整题切 numbered——不能一半按钮一半编号。"""
        labels = ["短", "这条选项特别长超过十六个字没跑了吧"]
        self.assertEqual(tg_ask.pick_layout(labels), "numbered")

    def test_thirty_latin_chars_are_fifteen(self):
        """拉丁按半字算：30 个字母＝15 字，还在按钮线内。"""
        self.assertEqual(tg_ask.pick_layout(["a" * 30]), "buttons")

    def test_explicit_layout_wins_both_ways(self):
        long_labels = ["这条选项特别长超过十六个字没跑了吧"]
        self.assertEqual(tg_ask.pick_layout(long_labels, "buttons"), "buttons")
        self.assertEqual(tg_ask.pick_layout(["A"], "numbered"), "numbered")

    def test_unknown_layout_rejected(self):
        with self.assertRaises(ValueError):
            tg_ask.pick_layout(["A"], "grid")


class Columns(unittest.TestCase):
    def test_single_letters_five_per_row(self):
        self.assertEqual(tg_ask.columns_for(["A", "B", "C", "D", "E"]), 5)

    def test_fewer_options_than_row_width(self):
        self.assertEqual(tg_ask.columns_for(["是", "否"]), 2)

    def test_medium_two_per_row(self):
        self.assertEqual(tg_ask.columns_for(["第一种方案", "第二种方案"]), 2)

    def test_long_one_per_row(self):
        self.assertEqual(tg_ask.columns_for(["十五个字的选项要独占一行的"]), 1)

    def test_explicit_wins_and_caps(self):
        self.assertEqual(tg_ask.columns_for(["A", "B"], 3), 3)
        self.assertEqual(tg_ask.columns_for(["A", "B"], 99), tg_ask.MAX_COLUMNS)


class KeyboardShape(unittest.TestCase):
    """callback_data 的 64 字节是 Telegram 的硬上限，超了整条消息 400。"""

    def test_callback_budget_at_max_options(self):
        labels = [f"选项{i}" for i in range(tg_ask.MAX_OPTIONS)]
        rows = tg_ask.build_keyboard(labels, "abcd1234", 5)
        for row in rows:
            for button in row:
                self.assertLessEqual(
                    len(button["callback_data"].encode()), 64,
                    f"超预算: {button['callback_data']}",
                )

    def test_option_text_stays_out_of_callback_data(self):
        label = "很长的选项文字" * 10
        rows = tg_ask.build_keyboard([label], "abcd1234", 1)
        self.assertNotIn(label, rows[0][0]["callback_data"])
        self.assertEqual(rows[0][0]["text"], label)

    def test_rows_shape(self):
        rows = tg_ask.build_keyboard(["A", "B", "C", "D", "E"], "ab12", 2)
        self.assertEqual([len(r) for r in rows], [2, 2, 1])

    def test_numbered_labels_emoji_then_plain(self):
        self.assertEqual(tg_ask.numbered_label(0), "1️⃣")
        self.assertEqual(tg_ask.numbered_label(9), "🔟")
        self.assertEqual(tg_ask.numbered_label(10), "11")


class ChoiceArgs(unittest.TestCase):
    def test_question_required(self):
        with self.assertRaises(ValueError):
            tg_ask.tool_ask_choice({"options": ["A"]}, "8888", FakeApi())

    def test_options_required(self):
        with self.assertRaises(ValueError):
            tg_ask.tool_ask_choice({"question": "q"}, "8888", FakeApi())

    def test_all_blank_options_rejected(self):
        with self.assertRaises(ValueError):
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["", "  "]}, "8888", FakeApi()
            )

    def test_too_many_options(self):
        labels = [str(i) for i in range(tg_ask.MAX_OPTIONS + 1)]
        with self.assertRaises(ValueError):
            tg_ask.tool_ask_choice(
                {"question": "q", "options": labels}, "8888", FakeApi()
            )

    def test_label_hard_limit(self):
        with self.assertRaises(ValueError):
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["x" * 301]}, "8888", FakeApi()
            )

    def test_timeout_bounds(self):
        for bad in (-1, tg_ask.MAX_TIMEOUT + 1, "abc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tg_ask.tool_ask_choice(
                    {"question": "q", "options": ["A"], "timeout_s": bad},
                    "8888", FakeApi(),
                )

    def test_numbered_body_over_text_limit_rejected(self):
        """长选项住正文，正文也有 4096 的顶。静默截断会把选项剪没，宁可报错。"""
        labels = [("选" * 250) + str(i) for i in range(19)]
        with self.assertRaises(ValueError):
            tg_ask.tool_ask_choice(
                {"question": "q", "options": labels}, "8888", FakeApi()
            )


class ChoiceFlow(unittest.TestCase):
    def test_drain_runs_before_send(self):
        """发题前必须先清积压——上一题的迟到点击不许算进这一题。"""
        api = FakeApi(plan=[[], lambda a: [a.click(0)]])
        ask(api)
        methods = [m for m, _ in api.calls]
        self.assertLess(methods.index("getUpdates"), methods.index("sendMessage"))
        first_poll = api.named("getUpdates")[0][1]
        self.assertEqual(first_poll.get("offset"), -1)

    def test_click_returns_index_option_message_id(self):
        api = FakeApi(plan=[[], lambda a: [a.click(1)]])
        result = ask(api)
        self.assertEqual(result, {"index": 1, "option": "B", "message_id": 111})

    def test_click_is_acked(self):
        """不 answerCallbackQuery，她的客户端会转圈十几秒。"""
        api = FakeApi(plan=[[], lambda a: [a.click(0)]])
        ask(api)
        self.assertTrue(api.named("answerCallbackQuery"))

    def test_stale_nonce_acked_expired_and_wait_continues(self):
        api = FakeApi(plan=[
            [],
            lambda a: [a.click(0, data="ask:deadbeef:0")],   # 上一题的幽灵按钮
            lambda a: [a.click(2)],
        ])
        result = ask(api)
        self.assertEqual(result["index"], 2)
        acks = api.named("answerCallbackQuery")
        self.assertIn("过期", acks[0][1].get("text", ""))

    def test_out_of_range_index_treated_as_stale(self):
        api = FakeApi(plan=[
            [],
            lambda a: [a.click(0, data=a.sent_keyboard()[0]["callback_data"]
                               .rsplit(":", 1)[0] + ":99")],
            lambda a: [a.click(0)],
        ])
        result = ask(api)
        self.assertEqual(result["index"], 0)

    def test_dm_rejects_other_users_click(self):
        """私聊 owner 校验：别人点了不算数、答 Not authorized，她点了才算。"""
        api = FakeApi(plan=[
            [],
            lambda a: [a.click(1, from_id="424242")],
            lambda a: [a.click(0, from_id="8888")],
        ])
        result = ask(api, chat="8888")
        self.assertEqual(result["index"], 0)
        acks = api.named("answerCallbackQuery")
        self.assertEqual(acks[0][1].get("text"), "Not authorized")

    def test_group_accepts_any_clicker(self):
        api = FakeApi(plan=[[], lambda a: [a.click(1, from_id="424242")]])
        result = json.loads(tg_ask.tool_ask_choice(
            {"question": "q", "options": ["A", "B"], "timeout_s": 5},
            "-100777", api,
        ))
        self.assertEqual(result["index"], 1)

    def test_offset_advances_past_consumed_updates(self):
        """吃过的 update 必须确认掉，不然同一下点击会被反复算。"""
        api = FakeApi(plan=[
            [],
            lambda a: [a.click(0, data="ask:deadbeef:0")],
            lambda a: [a.click(0)],
        ])
        ask(api)
        polls = api.named("getUpdates")
        self.assertEqual(polls[2][1].get("offset"), 9002)   # 幽灵点击 9001 + 1

    def test_mark_answered_default_edits_and_strips_keyboard(self):
        api = FakeApi(plan=[[], lambda a: [a.click(1)]])
        ask(api)
        edits = api.named("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertIn("已选：B", edits[0][1]["text"])
        self.assertNotIn("reply_markup", edits[0][1])   # 不带键盘＝键盘收掉

    def test_mark_answered_off_leaves_message(self):
        api = FakeApi(plan=[[], lambda a: [a.click(0)]])
        ask(api, mark_answered=False)
        self.assertFalse(api.named("editMessageText"))

    def test_timeout_is_tool_error_and_settles_card(self):
        api = FakeApi(plan=[[]])
        with self.assertRaises(RuntimeError) as ctx:
            ask(api, timeout_s=0)
        self.assertIn("没人点按钮", str(ctx.exception))
        edits = api.named("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertIn("超时", edits[0][1]["text"])

    def test_numbered_mode_puts_text_in_body_numbers_on_buttons(self):
        long_label = "这条选项特别长超过十六个字没跑了吧"
        api = FakeApi(plan=[[], lambda a: [a.click(1)]])
        result = ask(api, options=["短选项", long_label])
        self.assertEqual(result["option"], long_label)
        self.assertIn(long_label, api.sent_text())
        buttons = api.sent_keyboard()
        self.assertEqual([b["text"] for b in buttons], ["1️⃣", "2️⃣"])

    def test_409_names_dedicated_bot(self):
        api = FakeApi()
        original = api.__call__

        def flaky(method, data, files=None):
            if method == "getUpdates":
                raise ApiRejected("Conflict: terminated by other getUpdates", 409)
            return original(method, data, files)

        with self.assertRaises(RuntimeError) as ctx:
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["A"], "timeout_s": 5}, "8888", flaky
            )
        self.assertIn("专用 bot", str(ctx.exception))

    def test_non_409_rejection_passes_through(self):
        def broken(method, data, files=None):
            raise ApiRejected("Bad Request: chat not found", 400)

        with self.assertRaises(ApiRejected):
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["A"], "timeout_s": 5}, "8888", broken
            )

    # ---- B9：发题成功后轮询异常，也要 best-effort 收卡 ----
    def _poll_dies_api(self, calls, settle_fails=False):
        """_drain 那次放行；发题后第一次轮询就炸——模拟中途网络/409。"""
        def api(method, data, files=None):
            calls.append((method, dict(data)))
            if method == "getUpdates":
                if any(m == "sendMessage" for m, _ in calls):
                    raise RuntimeError("simulated polling timeout")
                return {"result": []}
            if method == "editMessageText" and settle_fails:
                raise RuntimeError("settle also broke")
            return {"result": {"message_id": 111}}
        return api

    def test_poll_error_settles_card_and_raises_with_id(self):
        # 反向变异：去掉 tool_ask_choice 里 _wait_click 的 try/except → 卡不收，本测试转红
        calls = []
        with self.assertRaises(RuntimeError) as ctx:
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["A", "B"]}, "8888",
                self._poll_dies_api(calls))
        methods = [m for m, _ in calls]
        self.assertIn("editMessageText", methods, "轮询炸了却没收卡，留了幽灵按钮")
        self.assertIn("111", str(ctx.exception), "失败响应要带题目 message_id")

    def test_mark_answered_false_skips_settle_but_still_raises(self):
        calls = []
        with self.assertRaises(RuntimeError) as ctx:
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["A", "B"], "mark_answered": False},
                "8888", self._poll_dies_api(calls))
        methods = [m for m, _ in calls]
        self.assertNotIn("editMessageText", methods,
                         "mark_answered=False 说自己管卡，不该替它收")
        self.assertIn("111", str(ctx.exception))

    def test_settle_failure_does_not_mask_original_error(self):
        calls = []
        with self.assertRaises(RuntimeError) as ctx:
            tg_ask.tool_ask_choice(
                {"question": "q", "options": ["A", "B"]}, "8888",
                self._poll_dies_api(calls, settle_fails=True))
        self.assertIn("simulated polling timeout", str(ctx.exception),
                      "收卡失败盖住了原始轮询错误")

    def test_nonce_unique_per_question(self):
        api1 = FakeApi(plan=[[], lambda a: [a.click(0)]])
        api2 = FakeApi(plan=[[], lambda a: [a.click(0)]])
        ask(api1)
        ask(api2)
        data1 = api1.sent_keyboard()[0]["callback_data"]
        data2 = api2.sent_keyboard()[0]["callback_data"]
        self.assertNotEqual(data1, data2)


class McpWiring(unittest.TestCase):
    """接进 server 的最后一寸：tools/list 可见、dispatch 可达。"""

    def setUp(self):
        self.had_chat = os.environ.get("TG_CHAT_ID")
        os.environ["TG_CHAT_ID"] = "8888"

    def tearDown(self):
        if self.had_chat is None:
            os.environ.pop("TG_CHAT_ID", None)
        else:
            os.environ["TG_CHAT_ID"] = self.had_chat

    def test_tools_listed(self):
        names = {t["name"] for t in mcp.TOOLS}
        self.assertIn("tg_ask_choice", names)
        self.assertEqual(mcp.KNOWN_TOOLS, names)

    def test_permission_tool_stays_offline(self):
        """tg_ask_permission 主动下架（没实弹验证过稳定性）——
        谁把它悄悄挂回 TOOLS，这条测试就来问设计账：先补实测，再上架。"""
        self.assertNotIn(
            "tg_ask_permission", {t["name"] for t in mcp.TOOLS}
        )

    def test_dispatch_reaches_tg_ask(self):
        api = FakeApi(plan=[[], lambda a: [a.click(0)]])
        original = mcp.call_api
        mcp.call_api = api
        try:
            response = mcp.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "tg_ask_choice", "arguments": {
                    "question": "q", "options": ["A", "B"], "timeout_s": 5,
                }},
            })
        finally:
            mcp.call_api = original
        body = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(body["option"], "A")

    def test_choice_timeout_surfaces_as_tool_error(self):
        api = FakeApi(plan=[[]])
        original = mcp.call_api
        mcp.call_api = api
        try:
            response = mcp.handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "tg_ask_choice", "arguments": {
                    "question": "q", "options": ["A"], "timeout_s": 0,
                }},
            })
        finally:
            mcp.call_api = original
        self.assertTrue(response["result"].get("isError"))


if __name__ == "__main__":
    unittest.main()
