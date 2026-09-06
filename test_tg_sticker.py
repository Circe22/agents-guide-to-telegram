#!/usr/bin/env python3
"""贴纸车道的测试。一条网络请求都不发（api/download 全是假的）。

跑法：python3 -m unittest test_tg_sticker -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True   # 防 pyc 缓存投毒（变异测试的教训）

import tg_sticker
from tg_sticker import ApiRejected

TOKEN = "12345:TESTTOKENAAAA"


def make_library(tmp: Path) -> list[dict]:
    """三张假贴纸：😾 池两张（其中一张 💻😾 可点名）、😭 池一张。"""
    (tmp / "img").mkdir(parents=True, exist_ok=True)
    entries = []
    for i, (title, emoji, aliases) in enumerate([
        ("敲键盘炸毛猫", "😾", ["💢", "💻"]),
        ("单纯生气猫", "😾", ["💢"]),
        ("大哭猫", "😭", ["🥹"]),
    ]):
        (tmp / "img" / f"{i:03d}.webp").write_bytes(b"RIFFfakewebp" + bytes([i]))
        entries.append({
            "id": i, "title": title, "desc": "", "tags": ["测试"],
            "emoji": emoji, "emojis": aliases,
            "file": f"img/{i:03d}.webp", "file_unique_id": f"UNIQ{i}",
        })
    (tmp / "library.json").write_text(
        json.dumps({"stickers": entries}, ensure_ascii=False), encoding="utf-8")
    return entries


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        os.environ["TG_STICKER_DIR"] = str(self.dir)
        os.environ.pop("TG_STICKER_MARKERS", None)
        os.environ.pop("TG_STICKER_MAX", None)

    def tearDown(self) -> None:
        os.environ.pop("TG_STICKER_DIR", None)
        self._tmp.cleanup()


class FakeApi:
    """记下每次调用；可按序注入 ApiRejected/RuntimeError。"""

    def __init__(self, fails=()):
        self.calls = []
        self.fails = list(fails)

    def __call__(self, method, data, files=None):
        self.calls.append((method, dict(data), files))
        if self.fails:
            exc = self.fails.pop(0)
            if exc is not None:
                raise exc
        return {"ok": True, "result": {
            "message_id": 7, "sticker": {"file_id": "FRESH", "file_unique_id": "UNIQ0"},
            "file_unique_id": "NEWUNIQ", "file_path": "stickers/file_9.webp",
            "file_size": 4096,
        }}


# ---------- emoji 处理 ----------
class TestClusters(Base):
    def test_zwj_whole(self):
        # ZWJ 组合字是一个簇，不许劈成两半
        self.assertEqual(tg_sticker.split_clusters("🧑‍💻"), ["🧑‍💻"])

    def test_two_emoji(self):
        self.assertEqual(tg_sticker.split_clusters("💻😾"), ["💻", "😾"])

    def test_skin_tone_sticks(self):
        self.assertEqual(tg_sticker.split_clusters("👍🏽😭"), ["👍🏽", "😭"])

    def test_flag_pair(self):
        self.assertEqual(tg_sticker.split_clusters("🇯🇵😭"), ["🇯🇵", "😭"])


class TestResolve(Base):
    def setUp(self):
        super().setUp()
        self.entries = make_library(self.dir)
        self.index = tg_sticker.key_index(self.entries)

    def test_single_pool(self):
        pool, combo = tg_sticker.resolve_emoji("😾", self.index)
        self.assertEqual({e["id"] for e in pool}, {0, 1})
        self.assertEqual(combo, "😾")

    def test_alias_entry(self):
        pool, _ = tg_sticker.resolve_emoji("🥹", self.index)
        self.assertEqual([e["id"] for e in pool], [2])

    def test_intersection_narrows_to_the_right_one(self):
        # 交集不是并集：💻😾 必须只剩「敲键盘炸毛猫」，不是三张里随机
        pool, _ = tg_sticker.resolve_emoji("💻😾", self.index)
        self.assertEqual([e["id"] for e in pool], [0])

    def test_order_normalized(self):
        _, k1 = tg_sticker.resolve_emoji("💻😾", self.index)
        _, k2 = tg_sticker.resolve_emoji("😾💻", self.index)
        self.assertEqual(k1, k2)   # 两种写法共用同一条「上次发过哪张」记忆

    def test_empty_intersection_refused(self):
        self.assertIsNone(tg_sticker.resolve_emoji("😭💻", self.index))

    def test_unknown_emoji_refused(self):
        self.assertIsNone(tg_sticker.resolve_emoji("🦑", self.index))
        self.assertIsNone(tg_sticker.resolve_emoji("😾🦑", self.index))

    def test_textual_refused(self):
        for s in ("挑眉", "1", "a", "😾 😭"):
            self.assertIsNone(tg_sticker.resolve_emoji(s, self.index))

    def test_variation_selector_stripped(self):
        pool, _ = tg_sticker.resolve_emoji("💢️", self.index)
        self.assertEqual({e["id"] for e in pool}, {0, 1})


class TestPick(Base):
    def setUp(self):
        super().setUp()
        self.entries = make_library(self.dir)

    def test_avoids_last(self):
        pool = [self.entries[0], self.entries[1]]
        seen = [tg_sticker.pick(pool, "😾")["id"] for _ in range(8)]
        for a, b in zip(seen, seen[1:]):
            self.assertNotEqual(a, b, "两张的池子必须避开上次那张")

    def test_single_pool_repeats_ok(self):
        pool = [self.entries[2]]
        self.assertEqual(tg_sticker.pick(pool, "😭")["id"], 2)
        self.assertEqual(tg_sticker.pick(pool, "😭")["id"], 2)


# ---------- 懒迁移状态机 ----------
class TestSendEntry(Base):
    def setUp(self):
        super().setUp()
        self.entries = make_library(self.dir)

    def test_no_cache_uploads_then_caches(self):
        api = FakeApi()
        note = tg_sticker.send_entry(self.entries[0], "111", TOKEN, api)
        self.assertIn("已从归档上传", note)
        method, data, files = api.calls[0]
        self.assertEqual(method, "sendSticker")
        self.assertIn("sticker", files)
        # 第二次直接走缓存，不再上传
        api2 = FakeApi()
        tg_sticker.send_entry(self.entries[0], "111", TOKEN, api2)
        _, data2, files2 = api2.calls[0]
        self.assertIsNone(files2)
        self.assertEqual(data2["sticker"], "FRESH")

    def test_400_invalidates_and_reuploads(self):
        tg_sticker.remember_file_id(TOKEN, "UNIQ0", "STALE")
        api = FakeApi(fails=[ApiRejected("API 拒收: wrong file id", 400), None])
        tg_sticker.send_entry(self.entries[0], "111", TOKEN, api)
        self.assertEqual(len(api.calls), 2)
        self.assertIsNotNone(api.calls[1][2], "400 之后必须走归档原图重传")
        self.assertEqual(tg_sticker.load_cache(TOKEN)["UNIQ0"], "FRESH")

    def test_network_error_is_not_invalidation(self):
        tg_sticker.remember_file_id(TOKEN, "UNIQ0", "GOOD")
        api = FakeApi(fails=[RuntimeError("发送失败: timeout")])
        with self.assertRaises(RuntimeError):
            tg_sticker.send_entry(self.entries[0], "111", TOKEN, api)
        self.assertEqual(len(api.calls), 1, "网络错不许触发重传")
        self.assertEqual(tg_sticker.load_cache(TOKEN)["UNIQ0"], "GOOD")

    def test_non_400_rejection_propagates(self):
        tg_sticker.remember_file_id(TOKEN, "UNIQ0", "GOOD")
        api = FakeApi(fails=[ApiRejected("API 拒收: forbidden", 403)])
        with self.assertRaises(ApiRejected):
            tg_sticker.send_entry(self.entries[0], "111", TOKEN, api)
        self.assertEqual(len(api.calls), 1)

    def test_unrelated_400_is_not_id_invalidation(self):
        """澄审 P2-3 点名的反例：400 是杂物袋，chat 错也报 400。

        cached file_id 本身没问题、错在别处时：不许触发归档重传（白传一遍
        还把真错因盖掉）、不许动缓存、异常原样出去让调用方看到真错误。
        """
        tg_sticker.remember_file_id(TOKEN, "UNIQ0", "GOOD")
        for desc in ["Bad Request: chat not found",
                     "Bad Request: PEER_ID_INVALID",
                     "Bad Request: message text is empty"]:
            with self.subTest(desc=desc):
                api = FakeApi(fails=[ApiRejected(desc, 400)])
                with self.assertRaises(ApiRejected):
                    tg_sticker.send_entry(self.entries[0], "111", TOKEN, api)
                self.assertEqual(len(api.calls), 1, "无关 400 不许触发重传")
                self.assertEqual(tg_sticker.load_cache(TOKEN)["UNIQ0"], "GOOD")

    def test_real_file_id_400_wordings_all_reupload(self):
        # 官方真实文案逐句过放行名单——名单改坏任何一句都得转红
        for desc in ["Bad Request: wrong file identifier/HTTP URL specified",
                     "Bad Request: wrong remote file identifier specified: xxx",
                     "Bad Request: FILE_REFERENCE_EXPIRED"]:
            with self.subTest(desc=desc):
                tg_sticker.remember_file_id(TOKEN, "UNIQ0", "STALE")
                api = FakeApi(fails=[ApiRejected(desc, 400), None])
                tg_sticker.send_entry(self.entries[0], "111", TOKEN, api)
                self.assertEqual(len(api.calls), 2)
                self.assertIsNotNone(api.calls[1][2], "点名 file_id 的 400 必须走重传")

    def test_archive_must_live_in_library(self):
        # 库外文件必须**真实存在**：不然圈禁被拆后会掉进「原图不见了」那条，
        # 异常类型一样，测试恒真假绿（变异测试逮出来的，别改回去）
        outside = self.dir.parent / "outside-archive.webp"
        outside.write_bytes(b"secret-adjacent bytes")
        try:
            evil = dict(self.entries[0], file=f"../{outside.name}")
            api = FakeApi()
            with self.assertRaises(RuntimeError) as ctx:
                tg_sticker.send_entry(evil, "111", TOKEN, api)
            self.assertIn("不在库目录内", str(ctx.exception))
            self.assertEqual(api.calls, [], "库外文件一个字节都不许出门")
        finally:
            outside.unlink()


# ---------- 工具层 ----------
class TestToolSend(Base):
    def setUp(self):
        super().setUp()
        make_library(self.dir)

    def test_no_args_lists(self):
        out = tg_sticker.tool_sticker_send({}, "", TOKEN, FakeApi())
        self.assertIn("馆藏 3 张", out)
        self.assertIn("敲键盘炸毛猫", out)

    def test_empty_library_lists_hint(self):
        os.environ["TG_STICKER_DIR"] = str(self.dir / "elsewhere")
        out = tg_sticker.tool_sticker_send({}, "", TOKEN, FakeApi())
        self.assertIn("空", out)

    def test_unknown_emoji_refuses_loudly(self):
        with self.assertRaises(ValueError):
            tg_sticker.tool_sticker_send({"emoji": "🦑"}, "111", TOKEN, FakeApi())

    def test_id_direct(self):
        out = tg_sticker.tool_sticker_send({"id": "2"}, "111", TOKEN, FakeApi())
        self.assertIn("大哭猫", out)

    def test_query(self):
        out = tg_sticker.tool_sticker_send({"query": "炸毛"}, "111", TOKEN, FakeApi())
        self.assertIn("敲键盘炸毛猫", out)


class TestImport(Base):
    def test_pending_then_claim(self):
        api = FakeApi()
        out = tg_sticker.tool_sticker_import(
            {"file_id": "FID_LONG_ENOUGH_ABC"}, TOKEN, api, lambda p: b"webpbytes")
        self.assertIn("待认领区", out)
        self.assertIn("NEWUNIQ", out)
        self.assertEqual(api.calls[0][0], "getFile")
        # 认领：不带 file_id，凭 unique 从待认领区入库
        out2 = tg_sticker.tool_sticker_import(
            {"file_unique_id": "NEWUNIQ", "title": "试验猫", "emoji": "🐱",
             "emojis": ["😺"], "tags": ["测试"]},
            TOKEN, FakeApi(), lambda p: b"")
        self.assertIn("入库", out2)
        lib = tg_sticker.load_library()
        self.assertEqual(lib[0]["title"], "试验猫")
        self.assertEqual(lib[0]["file_unique_id"], "NEWUNIQ")
        self.assertFalse(list((self.dir / "pending").glob("NEWUNIQ*")),
                         "认领后待认领区要清干净")
        # file_id 进了本 bot 缓存
        self.assertEqual(tg_sticker.load_cache(TOKEN)["NEWUNIQ"], "FID_LONG_ENOUGH_ABC")

    def test_direct_import_with_metadata(self):
        out = tg_sticker.tool_sticker_import(
            {"file_id": "FID2", "title": "直进猫", "emoji": "😼"},
            TOKEN, FakeApi(), lambda p: b"bytes")
        self.assertIn("入库", out)
        self.assertEqual(tg_sticker.load_library()[0]["emoji"], "😼")

    def test_reimport_recognized(self):
        tg_sticker.tool_sticker_import(
            {"file_id": "FID3", "title": "老熟人猫", "emoji": "🐈"},
            TOKEN, FakeApi(), lambda p: b"bytes")
        out = tg_sticker.tool_sticker_import(
            {"file_id": "FID3_NEW"}, TOKEN, FakeApi(), lambda p: b"bytes")
        self.assertIn("老熟人猫", out)
        self.assertNotIn("待认领区", out)

    def test_rejects_non_sticker_ext(self):
        api = FakeApi()
        api.calls  # noqa: B018
        def bad_api(method, data, files=None):
            return {"ok": True, "result": {
                "file_unique_id": "U", "file_path": "documents/evil.exe",
                "file_size": 10}}
        with self.assertRaises(ValueError):
            tg_sticker.tool_sticker_import({"file_id": "F"}, TOKEN, bad_api, lambda p: b"")


# ---------- 并发导入（跨进程事务）----------
class TestConcurrentImport(Base):
    """B1：多进程/多会话共用库目录时，并发导入不许互相覆盖。

    稳定并发测试——**不碰内部调度钩子**（不 patch load_library）。屏障放在
    download 回调（公开入参）上：所有线程同时下载完、同时挤向提交段，制造最大
    争用；有锁则逐个提交、N 张全在，无锁则读—改—写丢失更新（审查 B1 的病灶）。

    反向变异：把 tool_sticker_import 提交段的 `_CrossProcessLock` 去掉，
    此测试必转红（final 少于 N 张，或 id/原图撞车）。
    """

    def test_concurrent_imports_all_survive(self):
        import threading

        tg_sticker.save_library([])
        n = 6
        ready = threading.Barrier(n)
        errors: list[str] = []
        labels = [chr(ord("A") + i) for i in range(n)]

        def api(method, data, files=None):
            fid = str(data.get("file_id"))
            return {"ok": True, "result": {
                "file_unique_id": "U" + fid, "file_path": fid + ".webp",
                "file_size": 1}}

        def download(remote):
            # 下载完统一在屏障处集合，再一起冲提交段——最大化争用
            try:
                ready.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            return remote.encode()

        def worker(label):
            try:
                tg_sticker.tool_sticker_import(
                    {"file_id": label, "title": f"猫{label}", "emoji": "😺"},
                    TOKEN, api, download)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(x,), name=x) for x in labels]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"并发导入报错：{errors}")
        lib = tg_sticker.load_library()
        self.assertEqual(len(lib), n, f"有导入被覆盖丢掉了：{lib}")
        ids = [e["id"] for e in lib]
        self.assertEqual(len(set(ids)), n, f"编号撞车：{ids}")
        files = [e["file"] for e in lib]
        self.assertEqual(len(set(files)), n, f"原图路径撞车：{files}")
        for e in lib:
            self.assertTrue((self.dir / e["file"]).is_file(),
                            f"原图丢了：{e['file']}")

    def test_concurrent_same_sticker_dedups_to_one(self):
        # 同一张贴纸被两个进程同时导入：锁内重读去重，只入一次、不建两个号
        import threading

        tg_sticker.save_library([])
        ready = threading.Barrier(2)

        def api(method, data, files=None):
            return {"ok": True, "result": {
                "file_unique_id": "SAME", "file_path": "x.webp", "file_size": 1}}

        def download(remote):
            try:
                ready.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            return b"same-bytes"

        def worker(fid):
            tg_sticker.tool_sticker_import(
                {"file_id": fid, "title": "同一只", "emoji": "😺"},
                TOKEN, api, download)

        threads = [threading.Thread(target=worker, args=(f,)) for f in ("F1", "F2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        lib = tg_sticker.load_library()
        self.assertEqual(len(lib), 1, f"同一张贴纸被入库两次：{lib}")


# ---------- 句内标记 ----------
class TestSplitMessage(Base):
    def setUp(self):
        super().setUp()
        make_library(self.dir)

    def kinds(self, text):
        return [k for k, _ in tg_sticker.split_message(text)]

    def test_position_is_semantics(self):
        parts = tg_sticker.split_message("好气（😾）\n但没关系")
        self.assertEqual([k for k, _ in parts], ["text", "sticker", "text"])
        self.assertEqual(parts[0][1], "好气")
        self.assertEqual(parts[2][1], "但没关系")

    def test_trailing_marker(self):
        parts = tg_sticker.split_message("说完了（😭）")
        self.assertEqual([k for k, _ in parts], ["text", "sticker"])

    def test_stage_directions_untouched(self):
        parts = tg_sticker.split_message("好啊（挑眉）走吧")
        self.assertEqual(parts, [("text", "好啊（挑眉）走吧")])

    def test_footnote_number_untouched(self):
        self.assertEqual(self.kinds("见注（1）"), ["text"])

    def test_unknown_emoji_left_inline(self):
        parts = tg_sticker.split_message("嗯（🦑）嗯")
        self.assertEqual(parts, [("text", "嗯（🦑）嗯")])

    def test_empty_intersection_left_inline(self):
        self.assertEqual(self.kinds("哭着敲（😭💻）"), ["text"])

    def test_backtick_exempt(self):
        parts = tg_sticker.split_message("语法是 `（😾）` 这样")
        self.assertEqual([k for k, _ in parts], ["text"])

    def test_double_backtick_exempt(self):
        # B8：成对等长的反引号串是一个代码跨度，中间不喷贴纸
        # 反向变异：把 _FENCE_RE 换回 ```.*?```|`[^`\n]*` ，本测试转红
        text = "``（😾）``"
        self.assertEqual(tg_sticker.split_message(text), [("text", text)])

    def test_quad_backtick_exempt(self):
        text = "````（😾）````"
        self.assertEqual(tg_sticker.split_message(text), [("text", text)])

    def test_fence_exempt(self):
        parts = tg_sticker.split_message("```\n（😾）\n```")
        self.assertEqual([k for k, _ in parts], ["text"])

    def test_tilde_fence_exempt(self):
        text = "~~~\n（😾）\n~~~"
        self.assertEqual([k for k, _ in tg_sticker.split_message(text)], ["text"])

    def test_unclosed_fence_masks_to_eof(self):
        # 未闭合围栏吃到文末，里面的表情不喷贴纸
        self.assertEqual([k for k, _ in tg_sticker.split_message("```\n（😾）")], ["text"])

    def test_longer_closing_fence_boundary(self):
        # R7（收编审查反例）：闭合围栏可比起始更长（CommonMark §4.5）——块内不发、块后仍发。
        # 反向变异：把 _fenced_spans 的闭合改回「必须与起始等长」，较长的闭合围栏会被
        # 当作没闭合、吃到文末，块后的（😾）被误遮，本测试「块后仍发」转红。
        inside = tg_sticker.split_message("```\n（😾）\n````\n尾巴")
        self.assertNotIn("sticker", [k for k, _ in inside])   # 块内不发
        after = tg_sticker.split_message("```\ncode\n````\n说完了（😾）")
        self.assertIn("sticker", [k for k, _ in after])        # 较长闭合围栏之后仍发

    def test_inline_triple_backticks_boundary(self):
        # R7（收编审查反例）：行首 ```code``` 的 info string 含反引号≠块围栏起始，
        # 应按行内代码处理——只遮代码本身，后面正文照常喷贴纸。
        # 反向变异：去掉起始围栏「反引号 info string 不得含反引号」的判定，整行被当围栏
        # 起始、未闭合吃到文末，后面的（😾）被误遮，「代码外仍发」转红。
        after = tg_sticker.split_message("```code``` 说完了（😾）")
        self.assertIn("sticker", [k for k, _ in after])        # 代码外仍发
        inside = tg_sticker.split_message("```（😾）``` 后面无标记")
        self.assertNotIn("sticker", [k for k, _ in inside])    # 代码内不发

    def test_halfwidth_parens(self):
        self.assertEqual(self.kinds("好气(😾)"), ["text", "sticker"])

    def test_max_cap(self):
        os.environ["TG_STICKER_MAX"] = "2"
        parts = tg_sticker.split_message("（😾）（😭）（😾）")
        self.assertEqual([k for k, _ in parts].count("sticker"), 2)
        self.assertEqual(parts[-1], ("text", "（😾）"))

    def test_kill_switch(self):
        os.environ["TG_STICKER_MARKERS"] = "0"
        self.assertEqual(self.kinds("好气（😾）"), ["text"])

    def test_empty_library_passthrough(self):
        os.environ["TG_STICKER_DIR"] = str(self.dir / "elsewhere")
        self.assertEqual(self.kinds("好气（😾）"), ["text"])

    def test_intersection_marker_names_the_right_one(self):
        parts = tg_sticker.split_message("（💻😾）")
        pool, _, _ = parts[0][1]
        self.assertEqual([e["id"] for e in pool], [0])


# ---------- 入站识别 hook ----------
import tg_sticker_hook


class TestHookScan(Base):
    def setUp(self):
        super().setUp()
        make_library(self.dir)
        # 模拟某个 bot 的 file_id 缓存（入站 tag 通常只有 file_id，靠它反查身份）
        (self.dir / "file-ids.999.json").write_text(
            json.dumps({"UNIQ1": "CACHEDFID_AAAAAAAAAAAAAAAA"}), encoding="utf-8")

    @staticmethod
    def tag(**attrs):
        inner = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        return f'<channel source="telegram" {inner} ts="2026-08-13">'

    def test_known_by_unique_id(self):
        out = tg_sticker_hook.scan(self.tag(
            attachment_kind="sticker",
            attachment_file_id="F" * 24, attachment_unique_id="UNIQ2"))
        self.assertIn("大哭猫", out)
        self.assertIn("不用下载看图", out)

    def test_known_by_file_id_reverse_map(self):
        out = tg_sticker_hook.scan(self.tag(
            attachment_kind="sticker", attachment_file_id="CACHEDFID_AAAAAAAAAAAAAAAA"))
        self.assertIn("单纯生气猫", out)

    def test_unknown_prompts_import(self):
        fid = "NEWFID_" + "X" * 20
        out = tg_sticker_hook.scan(self.tag(
            attachment_kind="sticker", attachment_file_id=fid))
        self.assertIn("tg_sticker_import", out)
        self.assertIn(fid, out)

    def test_template_fid_rejected(self):
        # 回扫真库时认出的「贴纸」全是测试模板——形状闸挡这类假货
        out = tg_sticker_hook.scan(self.tag(
            attachment_kind="sticker", attachment_file_id="{fid}"))
        self.assertEqual(out, "")

    def test_non_sticker_attachment_ignored(self):
        out = tg_sticker_hook.scan(self.tag(
            attachment_kind="photo", attachment_file_id="F" * 24))
        self.assertEqual(out, "")

    def test_line_cap(self):
        prompt = "\n".join(self.tag(
            attachment_kind="sticker", attachment_file_id="F" * (24 + i))
            for i in range(6))
        self.assertEqual(len(tg_sticker_hook.scan(prompt).splitlines()),
                         tg_sticker_hook.MAX_LINES)

    def test_never_raises_on_garbage(self):
        for garbage in ("", "<channel", "attachment_kind=\"sticker\"", "純文字"):
            tg_sticker_hook.scan(garbage)   # 不炸即过


# ---------- R1：并发导入同一 unique，归档不许拷进被截断的 pending ----------
class TestSameStickerTruncation(Base):
    """R1（收编审查反例）：两个调用同时导入同一张贴纸时，B 把共享 pending 文件
    `open('wb')` 截断为零字节的窗口内，A 正好提交归档——归档必须仍是完整原图，
    不能拷到 B 截断出来的空文件。

    业务结果：两个调用都成功、库里一条记录、且**归档字节 == 下载到的原图**。
    反向变异：把 `tool_sticker_import` 提交段的 `_atomic_write_bytes(final, blob)`
    换回 `final.write_bytes(archive.read_bytes())`（回头读共享 pending），此测试
    必转红（归档变成 b''）。
    """

    def test_archive_not_copied_from_truncated_pending(self):
        tg_sticker.save_library([])
        a_has_lock, b_truncated, a_done = (threading.Event() for _ in range(3))
        original_enter = tg_sticker._CrossProcessLock.__enter__
        original_write = Path.write_bytes
        results, errors = [], []

        def enter(lock):
            value = original_enter(lock)
            if (threading.current_thread().name == "A"
                    and lock.lock_path.name == "library.json.lock"):
                a_has_lock.set()
                if not b_truncated.wait(3):
                    raise RuntimeError("B never opened pending file")
            return value

        def write(path, content):
            if (threading.current_thread().name == "B"
                    and path.name == "SAME.webp" and path.parent.name == "pending"):
                with path.open("wb") as out:   # 正常 write_bytes 就是先在这里截断
                    b_truncated.set()
                    if not a_done.wait(3):
                        raise RuntimeError("A never completed")
                    return out.write(content)
            return original_write(path, content)

        def api(method, data, files=None):
            return {"ok": True, "result": {
                "file_unique_id": "SAME", "file_path": "same.webp", "file_size": 18}}

        def worker(label):
            try:
                results.append(tg_sticker.tool_sticker_import(
                    {"file_id": label, "title": label, "emoji": "😺"},
                    TOKEN, api, lambda remote: b"VALID_STICKER_BYTES"))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                if label == "A":
                    a_done.set()

        with mock.patch.object(tg_sticker._CrossProcessLock, "__enter__", enter), \
                mock.patch.object(Path, "write_bytes", write):
            a = threading.Thread(target=worker, args=("A",), name="A")
            b = threading.Thread(target=worker, args=("B",), name="B")
            a.start()
            self.assertTrue(a_has_lock.wait(2))
            b.start()
            a.join(5)
            b.join(5)

        self.assertEqual(errors, [], errors)
        self.assertEqual(len(results), 2)
        lib = tg_sticker.load_library()
        self.assertEqual(len(lib), 1)
        archived = (tg_sticker.sticker_dir() / lib[0]["file"]).read_bytes()
        self.assertEqual(archived, b"VALID_STICKER_BYTES",
                         f"归档在 pending 被截断时拷了空内容：{archived!r}")


# ---------- R3：系统锁的活性/互斥（同进程 fcntl 版，非 fcntl 平台跳过） ----------
@unittest.skipUnless(tg_sticker._fcntl is not None,
                     "同进程两句柄互斥依赖 flock（fcntl-only）；跨进程语义见下方 subprocess 用例")
class TestLockSameProcess(Base):
    """R3（收编审查反例 test_live_lock_holder_cannot_be_evicted_by_age）：
    活持有者不因锁「年龄」被夺走。这是 fcntl 特有的同进程两句柄验证，Windows 走
    下面的独立进程用例。

    反向变异：给 `_CrossProcessLock` 重新加回「mtime 超龄就 unlink 夺回」，此测试
    转红（B 会夺锁成功）。
    """

    def test_live_holder_cannot_be_evicted_by_age(self):
        target = self.dir / "state.json"
        a = tg_sticker._CrossProcessLock(target)
        b = tg_sticker._CrossProcessLock(target)
        a.__enter__()
        acquired_b = False
        try:
            old = time.time() - 31          # 等价于活持有者被暂停超过旧的 30s 租约
            os.utime(a.lock_path, (old, old))
            with mock.patch.object(tg_sticker, "_LOCK_WAIT_SECONDS", 0.05):
                try:
                    b.__enter__()
                    acquired_b = True
                except TimeoutError:
                    pass
        finally:
            if acquired_b:
                b.__exit__(None, None, None)
            a.__exit__(None, None, None)
        self.assertFalse(acquired_b, "活持有者还攥着句柄，B 却按年龄夺到了锁")


# ---------- R3：系统锁的四场景（澄拍板·独立进程·跨平台） ----------
# 每个场景用**真实独立子进程**持锁，覆盖 Linux（本地 + CI）与 Windows（CI test-windows）。
# 锁由内核按打开的句柄记账：进程死了内核自动释放，靠时间猜死活的租约逻辑已删掉。
_LOCK_WORKER = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["LOCKTEST_PKG"])
import tg_sticker
target, held_flag, release_flag, mode = sys.argv[1:5]
lock = tg_sticker._CrossProcessLock(Path(target))
lock.__enter__()
Path(held_flag).write_text("held")
deadline = time.time() + 30
while not os.path.exists(release_flag) and time.time() < deadline:
    time.sleep(0.02)
if mode == "hardexit":
    os._exit(1)          # 异常退出：跳过 __exit__，靠内核释放句柄锁
lock.__exit__(None, None, None)
"""


class TestLockCrossProcess(Base):
    """R3 锁验收（澄的四场景，独立进程）。同时收编审查反例
    test_previous_owner_cannot_unlink_successors_lock 的业务结果：前持有者退出
    绝不破坏后来者的互斥，且固定锁文件从不被 unlink。

    这四条与同进程 fcntl 用例互补：那条证同进程两句柄互斥（Linux 特有），这四条
    用真实独立进程覆盖两个平台，且 Windows CI 也真跑。
    """

    def setUp(self):
        super().setUp()
        self.pkg = str(Path(tg_sticker.__file__).resolve().parent)
        self.target = self.dir / "state.json"

    def _spawn_holder(self, mode: str):
        held = self.dir / f"held.{mode}.{os.getpid()}.{time.time_ns()}"
        release = self.dir / f"release.{mode}.{os.getpid()}.{time.time_ns()}"
        env = {**os.environ, "LOCKTEST_PKG": self.pkg}
        proc = subprocess.Popen(
            [sys.executable, "-c", _LOCK_WORKER,
             str(self.target), str(held), str(release), mode],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self._reap, proc)
        return proc, held, release

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        if proc.stderr is not None:
            proc.stderr.close()

    @staticmethod
    def _wait_file(path: Path, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.02)
        return False

    def _try_acquire(self, wait_seconds: float) -> bool:
        lock = tg_sticker._CrossProcessLock(self.target)
        with mock.patch.object(tg_sticker, "_LOCK_WAIT_SECONDS", wait_seconds):
            try:
                lock.__enter__()
            except TimeoutError:
                return False
        lock.__exit__(None, None, None)
        return True

    def test_live_holder_not_evicted_however_long_we_wait(self):
        # 场景①：活持有者，等再久也夺不走。
        proc, held, release = self._spawn_holder("release")
        try:
            self.assertTrue(self._wait_file(held), "子进程没能拿到锁")
            self.assertFalse(self._try_acquire(1.5),
                             "活持有者还在，等待者却夺到了锁")
            self.assertTrue(self.target.with_name("state.json.lock").exists(),
                            "固定锁文件被谁 unlink 了")
        finally:
            release.write_text("go")
            proc.wait(timeout=10)
        # 持有者干净释放后，才轮得到我们
        self.assertTrue(self._try_acquire(5.0), "持有者释放后仍拿不到锁")

    def test_acquire_after_holder_hard_killed(self):
        # 场景②：强制终止（SIGKILL/TerminateProcess）持有者后，其他进程能取锁。
        proc, held, release = self._spawn_holder("release")
        try:
            self.assertTrue(self._wait_file(held), "子进程没能拿到锁")
            self.assertFalse(self._try_acquire(0.2), "被杀之前就不该拿到")
        finally:
            proc.kill()
            proc.wait(timeout=10)
        self.assertTrue(self._try_acquire(5.0),
                        "持有者被强杀、内核该释放锁了，却仍拿不到")

    def test_waiter_timeout_does_not_disturb_holder(self):
        # 场景③：等待者超时，现有持有者不受影响（不被夺、锁文件不被删）。
        proc, held, release = self._spawn_holder("release")
        try:
            self.assertTrue(self._wait_file(held), "子进程没能拿到锁")
            self.assertFalse(self._try_acquire(0.3), "等待者不该拿到活持有者的锁")
            # 等待者超时之后：持有者仍持有（再试一次仍超时），锁文件仍在。
            self.assertFalse(self._try_acquire(0.3), "等待者超时后持有者被夺走了")
            self.assertTrue(self.target.with_name("state.json.lock").exists(),
                            "等待者把持有者的锁文件删了")
        finally:
            release.write_text("go")
            proc.wait(timeout=10)
        self.assertTrue(self._try_acquire(5.0), "持有者干净释放后仍拿不到锁")

    def test_reacquire_after_abnormal_exit(self):
        # 场景④：持有者异常退出（跳过 __exit__），后来者能重新取锁；锁文件仍在
        #（收编 test_previous_owner_cannot_unlink_successors_lock 的业务结果）。
        proc, held, release = self._spawn_holder("hardexit")
        self.assertTrue(self._wait_file(held), "子进程没能拿到锁")
        release.write_text("go")           # 触发它 os._exit(1)
        proc.wait(timeout=10)
        self.assertNotEqual(proc.returncode, 0, "本场景要的是异常退出")
        self.assertTrue(self.target.with_name("state.json.lock").exists(),
                        "异常退出后固定锁文件不该消失")
        self.assertTrue(self._try_acquire(5.0), "前持有者异常退出后仍取不到锁")


if __name__ == "__main__":
    unittest.main()
