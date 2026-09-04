#!/usr/bin/env python3
"""sticker-spec conformance —— Python 实现 × 共享 golden fixtures。

跑法：python3 -m unittest test_conformance -v

这套 fixtures 是贴纸车道的**规格真源**（sticker-spec/fixtures/）：谁实现这套
标记语法（本仓 Python、别家 TS 补丁、未来任何移植），都拿同一份用例跑，
止住多实现漂移。runner 只做一件事：把用例喂给**完整管线**（split_message），
不复刻任何 resolver 逻辑——复刻出来的是手抄本，手抄本会漂移。

xfail 纪律（严格）：case.known["python"] 存在＝这条已知不符 spec 裁决，
失败记 xfail 不算错；**修好后它会 XPASS 并算失败**——提醒把 known 摘牌，
别让过期的免死金牌烂在 fixtures 里。
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

SPEC = Path(__file__).parent / "sticker-spec" / "fixtures"
IMPL = "python"


def _load(name: str) -> dict:
    return json.loads((SPEC / name).read_text(encoding="utf-8"))


def _shape(parts: list) -> list[str]:
    """split_message 的段序列 → 可比对的形状串（池按 title 排序，不看选中项）。"""
    out = []
    for kind, payload in parts:
        if kind == "text":
            out.append(f"text:{payload}")
        else:
            pool = payload[0]
            out.append("sticker:" + "|".join(sorted(str(e.get("title")) for e in pool)))
    return out


class Conformance(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        os.environ["TG_STICKER_DIR"] = str(d)
        os.environ.pop("TG_STICKER_MARKERS", None)
        os.environ.pop("TG_STICKER_MAX", None)
        (d / "library.json").write_text(
            json.dumps(_load("library.json"), ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        os.environ.pop("TG_STICKER_DIR", None)
        self._tmp.cleanup()

    def _check(self, name: str, known: str | None, want: list[str], got: list[str]) -> None:
        if known:
            if got == want:
                self.fail(f"XPASS：{name} 已符合 spec，请把 known.{IMPL} 从 fixtures 摘牌")
            print(f"  xfail[{name}]: {known}", file=sys.stderr)
            return
        self.assertEqual(want, got, name)

    def test_resolver_cases(self) -> None:
        for c in _load("resolver-cases.json")["cases"]:
            with self.subTest(c["name"]):
                text = f"（{c['input']}）"   # 包一层括号走完整管线
                got = _shape(tg_sticker.split_message(text))
                if c["expect"] is None:
                    want = [f"text:{text}"]   # 不认＝原样留在正文
                else:
                    want = ["sticker:" + "|".join(sorted(c["expect"]))]
                self._check(c["name"], (c.get("known") or {}).get(IMPL), want, got)

    def test_split_cases(self) -> None:
        for c in _load("split-cases.json")["cases"]:
            with self.subTest(c["name"]):
                got = _shape(tg_sticker.split_message(c["input"]))
                self._check(c["name"], (c.get("known") or {}).get(IMPL), c["expect"], got)


if __name__ == "__main__":
    unittest.main()
