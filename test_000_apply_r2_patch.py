"""Temporary first-pass CI harness.

unittest discovery imports this file before the regular test_* modules.  It applies
the candidate patch to the checkout, then every existing test runs against the
patched source.  The finalize job deletes this file before the PR is merged.
"""
from bootstrap_r2 import apply_source_patch

apply_source_patch()

import io
import json
import os
import tempfile
import unittest
from unittest import mock

import tg_rich_mcp as mcp


class R2SemanticScopeBootstrap(unittest.TestCase):
    def test_plain_text_literals_pass(self):
        mcp.guard_blocks([
            {"type": "pre", "text": "file:///etc/passwd"},
            {"type": "paragraph", "text": "example attach://f999999 only"},
        ])

    def test_real_media_and_url_semantics_still_reject(self):
        with self.assertRaisesRegex(ValueError, "attach"):
            mcp.guard_blocks(
                [{"type": "photo", "photo": {"media": "attach://f3"}}],
                media_count=1,
            )
        for blocks in (
            [{"type": "photo", "photo": {"media": "file:///etc/passwd"}}],
            [{"type": "paragraph", "text": [
                {"type": "url", "text": "x", "url": "file:///etc/passwd"}
            ]}],
        ):
            with self.subTest(blocks=blocks):
                with self.assertRaisesRegex(ValueError, "本地 scheme"):
                    mcp.guard_blocks(blocks)


class R2MediaCapabilityBootstrap(unittest.TestCase):
    def test_edit_and_draft_cannot_fake_media_paths(self):
        common = {
            "chat_id": "10001",
            "blocks": [{"type": "photo", "photo": {"media": "attach://f0"}}],
            "media_paths": ["/tmp/not-really-uploaded.jpg"],
        }
        with mock.patch.object(mcp, "call_api") as api:
            with self.assertRaisesRegex(ValueError, "只支持 tg_rich_send"):
                mcp.tool_edit({**common, "message_id": 1})
            with self.assertRaisesRegex(ValueError, "只支持 tg_rich_send"):
                mcp.tool_draft({**common, "draft_id": 1})
            api.assert_not_called()

    def test_send_real_file_grants_attach_capability(self):
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


class R2TransportBootstrap(unittest.TestCase):
    def test_oversized_line_is_drained(self):
        stream = io.BytesIO(b"x" * 200_000 + b"\n" + b'{"jsonrpc":"2.0"}\n')
        line, oversized = mcp._read_request_line(stream, 64)
        self.assertTrue(oversized)
        self.assertEqual(line, b"")
        line, oversized = mcp._read_request_line(stream, 64)
        self.assertFalse(oversized)
        self.assertEqual(line, b'{"jsonrpc":"2.0"}\n')

    def test_main_continues_after_oversized_request(self):
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

    def test_string_blocks_are_bounded_before_second_parse(self):
        raw = '[{"type":"paragraph","text":"' + ("x" * 1000) + '"}]'
        with mock.patch.dict(os.environ, {"TG_RICH_MAX_REQUEST_BYTES": "128"}), \
             mock.patch.object(mcp.json, "loads", wraps=mcp.json.loads) as loads:
            with self.assertRaisesRegex(ValueError, "解析前字节上限"):
                mcp.build_rich({"blocks": raw})
            loads.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
