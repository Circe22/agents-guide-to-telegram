#!/usr/bin/env python3
"""贴纸车道的测试。一条网络请求都不发（api/download 全是假的）。

跑法：python3 -m unittest test_tg_sticker -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
