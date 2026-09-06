# sticker-spec —— 贴纸标记语法的规格真源

一套「（emoji）」句内标记语法，不止一份实现在跑：本仓的 Python（`tg_sticker.py`）、
别家的 TS 补丁、未来任何移植。多实现各自演化，行为就会悄悄漂开——同一条正文，
这边发贴纸那边不发，用户看到的是玄学。这个目录用一份共享 golden fixtures 止住漂移：
**规格长在用例里，谁实现谁来跑，跑同一份。**

## 跑法

- Python（本仓）：`python3 -m unittest test_conformance -v`
- 其它实现：自带 runner，读 `fixtures/` 同一份 JSON。runner 只许把用例喂给
  **完整管线**（split 入口），不许把 resolver 逻辑复刻进 runner——抄本会漂移，
  漂移的测试比没有更坏。

## fixtures 约定

- `fixtures/library.json`：合成馆藏。`title` 是断言身份（全大写 token）；别放真贴纸。
- `fixtures/resolver-cases.json`：标记内容 → 候选池（titles 排序比对）或 null（不认）。
  runner 包一层全角括号走 split 管线执行。
- `fixtures/split-cases.json`：整条正文 → 段序列形状（`text:…` / `sticker:池`）。
- **断言的是池，不是选中项**——selector 的随机性/个性化不进 conformance；
  resolver 管正确性，selector 管个性，两层责任不混。

## xfail 纪律（严格）

case 带 `known: {"<impl>": "原因"}` ＝该实现已知不符 spec 裁决：失败记 xfail 不算错，
**修好后 XPASS 算失败**——提醒把 known 摘牌，免死金牌不许过期烂在 fixtures 里。

## 已知漂移账（2026-09-04 首立时抓到的）

| # | 差异 | Python | TS 补丁 | spec 裁决 | 修复归属 |
|---|---|---|---|---|---|
| 1 | `kind` 非 sticker 的条目 | 不滤，photo 也进池 | 滤掉 | **不进贴纸车道**（photo 有自己的车道） | Python（`known.python` 在案） |
| 2 | 变体选择符剥离 | VS15+VS16 都剥 | 只剥 VS16 | **两个都剥**（宽进） | TS（`known.ts` 在案） |
| 3 | 标记内容长度上限 | 32 码位 | 16 码位（`u` 模式量词按码位） | **统一 32** | TS（`known.ts` 在案） |
| 4 | 代码遮罩边界（多反引号/围栏起止/波浪线围栏） | ~~旧：`` ```.*?```|`[^`]*` `` 简易表达式~~ → 2026-09-07 先修为 Markdown 边界（B8），同日再按 **CommonMark §4.5** 收严（R7） | 待各自 runner 复核 | **按 CommonMark §4.5 遮**：① 行内反引号跨度成对等长（N 开 N 闭）；② 块围栏**闭合可比起始更长**（同字符、长度 ≥ 起始），未闭合吃到文末，`~~~` 同理；③ **反引号起始围栏的 info string 不得含反引号**——含了就不是围栏、按行内代码处理（否则会把 `` ```code``` `` 这类行内代码当围栏起始、遮住后面正文） | Python 已修（B8+R7）；fixtures 现含 `double_backtick_code_masked`/`quad_backtick_code_masked`/`tilde_fence_masked`/`unclosed_fence_masks_to_eof`，R7 追加 `longer_closing_fence_then_marker`/`longer_closing_fence_masks_inside`/`inline_triple_backtick_then_marker` 三条（含「代码内不发＋代码外仍发」）；TS 侧本次离线未跑、跑到红即照裁决修 |

不在 fixtures 覆盖内、属实现自由（暂不裁决，记录在案防误会）：

- **避重记忆的组合键**：Python 对标记内 emoji 去重后排序（`😭😭📖`→`📖😭`），
  TS 不去重（`📖😭😭`）；只影响「上次发过哪张」的记忆槽位，不影响候选池。
- **避重记忆的存储**：Python 落盘 `state.json`（跨进程），TS 进程内 Map（换窗清零）。
- **无命中时的全文空白**：Python 会 strip 首尾，TS 原样返回。fixtures 文本首尾
  一律不带空白绕开此差异。
- **配置面**：env 名不同（`TG_STICKER_MAX` vs `JIE_STICKER_MAX` 等），属部署差异。

## 改 fixtures 的规矩

- 加 case 先想清 spec 裁决写进 `note`/本表，别让用例替你偷偷立法。
- 改期望值必须两端 runner 都跑过（一端绿一端红＝你刚立了单边法）。
- fixtures 曾做过变异验证（改坏期望→两端真红→还原）；再动 runner 记得重来一次。
