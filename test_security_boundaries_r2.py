import io
import json
import os
import tempfile
import unittest
from pathlib import Path
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
        # Windows 的 NamedTemporaryFile 默认持有独占句柄；load_media 再 open 会被拒。
        # 用目录 + 已关闭普通文件，测试的是产品代码的跨平台读路径。
        with tempfile.TemporaryDirectory() as td:
            media_path = Path(td) / "pic.jpg"
            media_path.write_bytes(b"jpg")
            args = {
                "chat_id": "10001",
                "blocks": [{"type": "photo", "photo": {"media": "attach://f0"}}],
                "media_paths": [str(media_path)],
            }
            with mock.patch.object(
                mcp, "call_api", return_value={"result": {"message_id": 7}}
            ) as api:
                mcp.tool_send(args)
            self.assertEqual(api.call_args.args[0], "sendRichMessage")
            self.assertIn("f0", api.call_args.kwargs["files"])


class TransportByteBound(unittest.TestCase):
    def test_oversized_line_is_drained_and_next_request_survives(self):
        stream = io.BytesIO(b"x" * 200_000 + b"\n" + b'{"jsonrpc":"2.0"}\n')
        line, oversized = mcp._read_request_line(stream, 64)
        self.assertTrue(oversized)
        self.assertEqual(line, b"")
        line, oversized = mcp._read_request_line(stream, 64)
        self.assertFalse(oversized)
        self.assertEqual(line, b'{"jsonrpc":"2.0"}\n')

    def test_limit_counts_utf8_bytes(self):
        stream = io.BytesIO("猫猫\n".encode("utf-8"))
        _line, oversized = mcp._read_request_line(stream, 6)
        self.assertTrue(oversized)

    def test_main_returns_error_then_continues(self):
        oversized = b"x" * 100 + b"\n"
        ping = b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n'

        class FakeStdin:
            def __init__(self, data: bytes):
                self.buffer = io.BytesIO(data)

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "64"}), \
             mock.patch.object(mcp.sys, "stdin", FakeStdin(oversized + ping)), \
             mock.patch.object(mcp.sys, "stdout", out):
            mcp.main()
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["id"], 9)
        self.assertEqual(responses[1]["result"], {})

    def test_blocks_string_is_bounded_before_second_json_parse(self):
        raw = '[{"type":"paragraph","text":"' + ("x" * 1000) + '"}]'
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "128"}), \
             mock.patch.object(mcp.json, "loads", wraps=mcp.json.loads) as loads:
            with self.assertRaisesRegex(ValueError, "解析前字节上限"):
                mcp.build_rich({"blocks": raw})
            loads.assert_not_called()

    def test_bad_env_falls_back(self):
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "nope"}):
            self.assertEqual(mcp._request_max_bytes(), mcp.REQUEST_MAX_BYTES_DEFAULT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
