# The Agent's Guide to Telegram

**教 agent 使用 Telegram：发原生表格、LaTeX 公式、折叠块、真贴纸，让你在手机上看着它干活——外加一整本真机踩出来的坑谱。**

> <https://github.com/Circe22/agents-guide-to-telegram> · MIT · 只依赖 `requests`
> 发现 bug 或者 Telegram 又更新了，欢迎开 issue / 提 PR。
> （前名 `tg-rich-mcp`，旧链接与旧 git remote 均自动跳转。名字致敬你猜到的那本书——
> 面对一个陌生星球的 API，手册比勇气有用，Don't Panic.）

Telegram 在 Bot API **10.1**（2026-06-11）加了 Rich Messages，10.2（07-14）补齐发送侧。
官方 telegram 插件的 `reply` 够不着这些，这个包直投 Bot API 把它接进来。

两件东西，可以只用一件：

| 文件 | 是什么 | 通用性 |
|---|---|---|
| `tg_rich_mcp.py` | MCP server，六个工具：发 / 原地改 / 推草稿 / 贴纸挑发 / 贴纸入库 / 按钮选择题（实验性） | ✅ 走**握手式** MCP 的 host（Claude Code、Claude Desktop、Cursor、自己写的 agent）——协议版本见下 |
| `tg_sticker.py` | 贴纸车道：库 / 认领 / 交集挑选 / 各 bot 懒迁移 / 句内标记（挂在上面那个 server 里） | 跟着走 |
| `tg_ask.py` | 按钮问答机制层：inline keyboard + getUpdates 同步等点击（挂在上面那个 server 里） | 跟着走；⚠️ 要**专用 bot**，见「按钮问答要专用 bot」 |
| `tg_progress_hook.py` | 进度窗 hook：每次调工具前推一行 | ⚠️ **仅 Claude Code**（靠它的 PreToolUse 钩子，别的 host 没有这个机制），且需要 Linux / macOS / WSL |
| `tg_sticker_hook.py` | 入站贴纸识别 hook：认识的注入标签（agent 不用看图）、不认识的提醒归档 | ⚠️ **仅 Claude Code**（UserPromptSubmit 钩子）；零网络、fail-silent |
| `secret_redaction.py` | 密钥形态的单一真源，上面的都用它 | 跟着走，别单独删 |
| `sticker-spec/` | 贴纸标记语法的规格真源：共享 golden fixtures，多实现各自跑同一份止漂移（见 COOKBOOK 贴纸章末节） | ✅ 任何实现这套标记语法的都该跑 |
| `test_*.py` | 一批测试（含 `test_conformance.py` 跑 sticker-spec），`python3 -m unittest discover -v`，**无需 Telegram 凭证、不发网络**；耗时取决于环境 | — |

外加一份 **[COOKBOOK.md](COOKBOOK.md)** —— Telegram 富消息**能玩什么**的全景清单：
行内公式、剧透、上下标、脚注、锚点跳转、任务清单、表格高级字段、地图、拼贴轮播、
按读者时区渲染的时间……包括你大概率用不上的那些。
列出来不是让你都用，是让你**知道有这条路**——不知道的能力等于不存在。

进度窗长这样，在手机上一行行自己长出来：

```
┌ 正在干活…
│ 📖 Read · server.py
│ 🔍 Grep · handleRequest
│ ⚡ Bash · 跑一遍测试
└ 已经做了 12 步
```

> **它默认会把长得像密钥的摘要替换成「（内容隐去）」。不喜欢这种防御？
> `TG_PROGRESS_REDACT=0` 一把关掉** —— 详见下面「安全闸，以及怎么关」。

> **TL;DR (English)** — Teach your agent to use Telegram. An MCP server exposing Telegram's Rich Message API
> (native tables, LaTeX, collapsible blocks, in-place edits, streaming drafts,
> an emoji-indexed sticker lane the agent curates itself,
> plus experimental synchronous choice questions answered with one button tap)
> to any handshake-based stdio MCP host (protocol 2024-11-05 … 2025-11-25),
> plus a Claude Code hook that streams your agent's tool calls
> into a live Telegram window. Config via `~/.tg-rich-mcp.json`.
> Only dependency: `requests`. Redaction is on by default — `TG_PROGRESS_REDACT=0` disables it.
>
> ⚠️ **Android caveat**: while a streaming draft is active, Telegram Android replaces the
> user's send button with an ellipsis — they cannot send anything, and text already typed
> gets wiped when the input recovers ([bugs.telegram.org/c/62189](https://bugs.telegram.org/c/62189),
> closed by Telegram as expected behaviour). Bot API 10.3 added a partial fix: drafts sent
> with `can_stop` show a Stop button on **up-to-date** clients — pressing it dismisses the
> draft and unlocks the composer — but the bot can't hear the press unless its inbound side
> handles `stopped_message_generation`, and older clients never draw the button.
> The progress hook therefore still defaults to `sendRichMessage` + `editMessageText`
> and deletes the window when done; drafts (frames sent with `can_stop`) are opt-in
> via `TG_PROGRESS_MODE=draft`.

---

## 装

### 1. 配置

不走代理的最小配置（可直接复制）：

```bash
cat > ~/.tg-rich-mcp.json <<'JSON'
{
  "bot_token": "123456:AA...",
  "chat_id": "你的 chat id"
}
JSON
chmod 600 ~/.tg-rich-mcp.json
```

要走代理就**加一行** `"proxy"`——JSON 不允许尾逗号，所以给它**前面**那行（`chat_id`）
补上一个逗号：

```json
{
  "bot_token": "123456:AA...",
  "chat_id": "你的 chat id",
  "proxy": "http://127.0.0.1:7897"
}
```

反过来，要删掉末尾某个字段（如 proxy），记得连同**上一行的逗号**一起删，别留下
`"chat_id": "…",` 这种悬着的尾逗号——那是不合法的 JSON。也支持环境变量
`TG_BOT_TOKEN` / `TG_CHAT_ID` / `TG_PROXY`（优先级更高）。
配置在进程启动时读一次并缓存，**改了要重启 MCP server 才生效**。
配置文件**不存在**时静默按"没配"处理；**存在但格式错误**会往 stderr 打一行脱敏诊断
（不吞掉、也不把 token 带出来），免得只看到"没找到 token"却不知道是 JSON 写坏了。

> **想用进度窗 hook 的话，必须用配置文件**（或把变量 export 进编辑器的启动环境）——
> hook 是编辑器另起的进程，拿不到你写在 MCP server 那段 `env` 里的变量。
> 这是最容易卡住的一步，第一次装的人十有八九栽在这。

### 2. 挂 MCP server

`.mcp.json`（或你的 host 对应的配置文件）：

```json
{
  "mcpServers": {
    "tg-rich": {
      "command": "python3",
      "args": ["/绝对路径/tg_rich_mcp.py"]
    }
  }
}
```

只依赖 `requests`，协议是手写的 JSON-RPC over stdio，不需要 mcp SDK。完整安装：

```bash
git clone https://github.com/Circe22/agents-guide-to-telegram.git
cd agents-guide-to-telegram
python3 -m venv .venv && . .venv/bin/activate   # Python 3.9+
pip install requests
```

⚠️ **`.mcp.json` 里的 `command` 要指向同一个解释器**——上面装 `requests` 用的 venv
里的 `python`（即 `/绝对路径/.venv/bin/python`），别写成系统 `python3`，否则 MCP
起的进程用的是另一套环境、`import requests` 失败。用哪个解释器装、就用哪个解释器起。

#### 支持哪几版 MCP 协议

实现的是**握手式**（`initialize` / `notifications/initialized`）的 MCP，协商这四版：

```
2025-11-25 · 2025-06-18 · 2025-03-26 · 2024-11-05
```

客户端要的版本在这里面就回同一个，不在就回最新的那个、由它决定断不断。

> ⚠️ **2026-07-28 那版不支持**，而且不是"再加一个字符串"就能支持的——它把 MCP 改成了
> **无状态**协议：移除 `initialize` 握手，协议版本和客户端能力改为每个请求放进 `_meta`；
> 服务器 MUST 实现 `server/discover`；所有 result 必须带 `resultType`；
> `ping` / `logging/setLevel` 被移除。
> （[Key Changes](https://modelcontextprotocol.io/specification/2026-07-28)）
>
> 好在规范留了向后兼容的路：新客户端**可以**先拿 `server/discover` 探测、
> 失败再按旧握手回退——本 server 对它回 `method not found`，这条回退路即成立
> （本 server 的响应实测是这个）。**但"是否真去探测、真回退"取决于客户端实现**，
> 不是所有同时支持两代协议的 host 都一定这么做；只实现了 2026-07-28 的客户端更
> 不在此列，连不上这个 server——这不是 bug，是两代协议的分界。
> 真要支持新协议是另一个工程，欢迎提 PR。

### 3. 挂进度窗 hook（可选，仅 Claude Code）

> ⚠️ **进度窗 hook 需要 Linux / macOS / WSL。** 它用 `fcntl` 给状态文件加锁
> （并发的工具调用会同时写同一个文件），而 `fcntl` 是 Unix-only ——
> **原生 Windows 的 Python 一 import 就报错**。
> Windows 用户请在 WSL 里跑 Claude Code，或者只用 MCP server 那半边（那半边全平台都行）。
>
> Progress hook requires Linux, macOS, or WSL (`fcntl` is Unix-only).
> The MCP server itself runs anywhere.

`.claude/settings.json`：

```json
{
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command",
      "command": "python3 /绝对路径/tg_progress_hook.py",
      "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "python3 /绝对路径/tg_progress_hook.py --finish",
      "timeout": 15}]}]
  }
}
```

改完要重开会话才生效。

前一条是每一帧，后一条是收工。只挂前一条也能用，只是窗口会停在最后一帧、不会自己收拾。

### 4. 挂贴纸识别 hook（可选，仅 Claude Code）

收贴纸不该靠 agent「记得」。挂上这个 hook 之后：用户发来**库里认识的**贴纸，
agent 直接收到标题/emoji/标签/描述，不用下载看图；**没见过的**，注入一行提醒
（file_id 已带好），得空调一次 `tg_sticker_import` 就归档。

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python3 /绝对路径/tg_sticker_hook.py",
      "timeout": 5}]}]
  }
}
```

它**零网络**（识别只查本地库，下载留给 import 工具）、fail-silent（自己挂了
最多少一行提示，绝不挡用户说话）。前提：入站消息里有 `attachment_kind="sticker"`
和 `attachment_file_id`（官方 telegram 插件的 tag 格式）；只有 file_id 时靠
import 时记下的各 bot 缓存反查身份，所以**导入过的才认得出**。

#### 它默认怎么干活

**发一条正式消息，然后每帧原地改它**（`sendRichMessage` → `editMessageText`），
收工时把这条消息**撤掉**——聊天记录里一条工具调用都不留。

> ⚠️ **为什么默认不是流式草稿**：`sendRichMessageDraft` 活跃期间，
> **Telegram Android 会把用户的发送键换成省略号，用户发不出消息，
> 而且这期间在输入框里打的字会在恢复时被清空。**
> 官方缺陷记录 <https://bugs.telegram.org/c/62189> 已被关闭，称是"当前预期行为"。
> Bot API 10.3 起本 hook 的草稿帧都带 `can_stop`——**新客户端**有停止按钮，
> 按停＝草稿消失+输入框解锁（2026-09-04 Android 实测）。但发布出去的 hook
> 没法预知用户拿的是哪版客户端：**旧客户端不画这颗按钮**、锁死照旧；
> 且 hook 收不到按停事件（`stopped_message_generation` 走收信侧）——
> 好在进度窗瞎推无害，按停后客户端会把同 draft_id 的后续帧直接扔掉。
>
> 长任务里用户最需要插话的时刻（补条件、喊停、纠方向、回答 agent 的提问），
> 恰好就是草稿最活跃的时刻。所以草稿仍只在你显式打开时才走——
> 确认你的用户客户端够新（或在桌面端）再开。
> 桌面端据用户反馈不锁输入框——那是**用户反馈，不是官方的跨平台保证**。

| 想要什么 | 怎么设 |
|---|---|
| 默认（持久窗 + 收工撤掉） | 什么都不用设 |
| 干完把窗口留下来当记录 | `TG_PROGRESS_END=keep` |
| 就要那种流式动画+自动蒸发（帧自带 can_stop；**旧客户端仍锁输入框**） | `TG_PROGRESS_MODE=draft` |
| 换标题 | `TG_PROGRESS_TITLE=…` / `TG_PROGRESS_DONE_TITLE=…` |

**不用改 bot、不用升级什么**——Rich Message 是 Telegram 服务端的能力，
你的 bot 直接调新方法就有。客户端得是支持 10.1 的版本才看得到渲染效果。

---

## 安全闸，以及怎么关

进度窗要把工具调用的摘要发进 Telegram，所以默认带两道闸。
**它们都可以关，而且关得干脆——这是你的机器。**

| 想干什么 | 怎么做 |
|---|---|
| 整个进度窗都不要 | `TG_PROGRESS=0` |
| 要进度窗，但**不要任何脱敏**（摘要原样推） | `TG_PROGRESS_REDACT=0` |
| 想知道哪些东西被隐过 | 看 `~/.tg-progress/redacted.log` |
| 换掉窗口标题 | `TG_PROGRESS_TITLE="你的标题"` / `TG_PROGRESS_DONE_TITLE="…"` |
| 干完别删、留一条记录 | `TG_PROGRESS_END=keep` |
| 要流式草稿（**会锁安卓输入框**） | `TG_PROGRESS_MODE=draft` |

两道闸分别是：

1. **关键词闸** —— 摘要里出现 `token` / `secret` / `password` / `.env` / `id_rsa` … 就整条隐去。
2. **形态闸** —— 认长相不认词：`sk-*`、`AKIA*`、`ghp_*`、`xox?-*`、JWT、PEM 头、
   长 hex / base64、URL 里的 `user:pass@`。
   只有关键词闸是不够的：`deploy sk-live-ABC123XYZ` 一个关键词都没有，照样是把密钥递出去。

另外几处是硬编码的保守取舍（不受开关影响之外的行为，代码里改也就一行）：

- `Bash` 只发 `description`（人话说明），**从不发 `command` 全文**。
- `Grep`/`Glob` 的 pattern 只在长得像普通标识符时才发，否则一个字不说。
- `WebFetch` 的 URL 只留 host + path，丢掉 query / userinfo / fragment。

**关了会怎样**：Bash 的说明、文件名、搜索词会原样发进 Telegram。
如果你的 agent 会碰到真的生产密钥，想清楚再关。

`redacted.log` **刻意不记原文**——记了就等于把密钥抄进另一个文件，闸就白设了。
它只记时间、哪个工具、命中哪道闸、原摘要多长，够回头对账。

---

## 用

### 日常：markdown 一行字

```
tg_rich_send(markdown="## 今日进度\n\n- [x] 修完 bug\n- [ ] 写测试")
```

### 进阶：把它接成 agent 的默认出口（渲染器模式）

「记得挑富消息工具、选对格式」不该是 agent 每条消息的负担。更稳的接法是反过来：
**正式文字回复默认走 `tg_rich_send(markdown=…)`**，把它当渲染器用——
没有 Markdown 语法的消息渲染出来就是普通文本，写了 `**重点**`、列表、代码块的
自动长成原生样式，agent 不用每条都想"这条要不要富格式"。

> ⚠️ **这是"调用方需要自己实现"的接入策略，不是本工具内置的行为**：`tg_rich_send`
> 本身**没有** sendMessage 自动降级；server instructions 也仍让纯文字聊天走原来的
> 发送工具。下面两条护栏是给"要照这个思路接"的调用方的接入约定，得你自己在外层写。

接入示例（伪码）：

```text
try:
    tg_rich_send(markdown=reply)
except err:
    if 是"格式/方法不支持"类拒收:   # 见护栏 1 的辨别
        sendMessage(text=reply)      # 降级重发同一段
    else:
        raise                        # 其它错不降级
```

两条护栏（这个接法实跑出来的，别省）：

1. **降级只认"格式/方法不支持"类拒收，`400 / 404` 只是候选范围不是判据**：Telegram
   因富格式/能力问题拒收时才退回普通 `sendMessage` 重发同一段。但 `400` 是杂物袋——
   `chat not found`、`reply message not found`、`message text is empty` 也报 400，
   `404` 也可能是 token/路径错；这些**不该**降级成纯文本（降了也发不出、还盖掉真错因）。
   要辨别的是"这段富消息本身不被接受"，不是"这次请求有别的毛病"。
   **网络错、超时、5xx、429 一律原样抛错，不自动补发**——这些状态下 Telegram 可能
   已经收到了第一条，自动补发＝制造重复消息。
2. **只包纯文字出口**：引用回复、附件、贴纸等旁路照走原来的路，别把整条发送链
   都塞进渲染器——包的面越大，降级时要复原的状态越多。

### 长任务：持久进度窗（推荐）

```
tg_rich_send(blocks=[...])        → 返回 message_id
tg_rich_edit(blocks=[...])        ← 每帧原地改；message_id 可省，
                                     默认改本会话最后发的那条（簿记归脚本）
```

`editMessageText` 收 `rich_message`（10.1 加的），所以进度窗**不必**用 30 秒草稿：
发一条正式消息、之后原地编辑，留在聊天记录里、编辑还不响铃。

`tg_rich_draft` 只在你要那种 30 秒动画质感时才用（私聊限定，不进聊天记录，
定稿必须补一条正式消息）。**它会锁住安卓用户的发送框**——新客户端可以
`can_stop: true` 给用户一颗解锁按钮，但旧客户端不画按钮、bot 也收不到按停
事件（完整账见坑 3），所以仍别拿它当长任务的默认进度方案。
进度窗 hook 的两种形态用 `TG_PROGRESS_MODE` 切：`edit`（默认·持久窗）/
`draft`（流式动画·帧自带 can_stop·收工自动消失）——各有拥趸，都留着。

### 表格 / 公式：用 blocks

```json
[
  {"type": "heading", "size": 3, "text": "本周开销"},
  {"type": "table", "is_bordered": true, "is_striped": true,
   "cells": [
     [{"text": "项目", "is_header": true}, {"text": "金额", "is_header": true}],
     [{"text": "服务器"}, {"text": "¥128"}],
     [{"text": "域名"}, {"text": "¥55"}]
   ]},
  {"type": "mathematical_expression", "expression": "\\sum_{i=1}^{n} x_i = 183"},
  {"type": "details", "summary": "明细", "blocks": [
     {"type": "paragraph", "text": "折叠起来只占一行。"}
  ]}
]
```

`mathematical_expression` 的 `expression` 是**裸 LaTeX**，不要包 `$$`。

### 本地图片 / 九宫格：media_paths + attach://

```
tg_rich_send(
  media_paths=["/pics/1.jpg", "/pics/2.jpg", "/pics/3.jpg"],
  blocks=[{"type": "collage", "blocks": [
    {"type": "photo", "photo": {"type": "photo", "media": "attach://f0"}},
    {"type": "photo", "photo": {"type": "photo", "media": "attach://f1"}},
    {"type": "photo", "photo": {"type": "photo", "media": "attach://f2"}}
  ]}]
)
```

- 第 i 个路径＝`attach://f{i}`。`collage` 换成 `slideshow` 就是左右翻页；
  单个 `photo` 块就是普通发图。一条消息最多 50 个媒体。**三个"上限"不是一回事，别混**：
  - **本地拦截阈值**：本工具对每个文件按 50 MiB 拦（`MEDIA_MAX_BYTES`），是内存/误传保护，不是 Telegram 的承诺；
  - **累计输入预算**：一次调用所有文件读进内存拼 multipart 的总量，默认 200 MiB（`TG_RICH_MEDIA_TOTAL_MB` 可调）；
  - **Telegram 自己的类型上限**：走 multipart 上传，**照片 10MB、其它文件 50MB**（[Sending Files](https://core.telegram.org/bots/api#sending-files)）。所以一张 50MB 的"照片"过得了本地阈值，却发不出去——别把"≤50MB"当成任意图片都能发。
- 发送成功的返回里带每个媒体的 **file_id**。存下来，下次 `media` 直接填
  file_id 复用，不用重新上传。**复用时整串程序化取用，别看着截断的显示手补
  尾巴**——file_id 彼此长得几乎一样，手打命中纯靠运气（作者试过，侥幸没炸）。
- 文件名形态闸：`.env` / `id_rsa` / `*.pem` / 名字含 token·credential·secret
  之类的文件会被拒，符号链接按**真实目标**检查。会误伤 `my_secret_santa.jpg`
  这种名字——确认无害就设 `TG_RICH_MEDIA_GUARD=0`。

### 贴纸：agent 的脸（两层）

思路、身份三定律和判断标准见 [COOKBOOK「贴纸」一章](COOKBOOK.md)；这里只讲用法。
库默认在 `~/.tg-rich-mcp-stickers/`（`TG_STICKER_DIR` 或配置文件 `sticker_dir`
可改），**空库时两层都零开销、零打扰**。

**第一层：工具对。**

```
tg_sticker_send()                            ← 不带参数＝看馆藏清单
tg_sticker_send(emoji="😾")                  ← 那一池里随机（避开上次刚发的那张）
tg_sticker_send(emoji="💻😾")                ← 交集收窄，通常两个 emoji 就点名一张
tg_sticker_import(file_id="…")               ← getFile 下载归档 → 返回原图路径，看图后……
tg_sticker_import(file_unique_id="…",
                  title="…", emoji="…")      ← ……再来认领入库（emojis 别名越多越容易命中）
```

**第二层：句内标记，渲染器模式的顺风车。** `tg_rich_send` 的 markdown 正文里
写 `（emoji）`，那个位置就发一张库里的真贴纸——写到哪儿，脸跟在哪条后面
（位置即语义）。已经按「渲染器模式」把正式回复路由过来的 agent，什么都不用改
就有了这层。

| 开关 | 默认 | 作用 |
|---|---|---|
| `TG_STICKER_MARKERS` | 开 | `=0` 关掉句内标记层（工具对不受影响） |
| `TG_STICKER_MAX` | 3 | 一条消息最多剥几张，多出来的原样留在正文 |
| `TG_STICKER_DIR` | `~/.tg-rich-mcp-stickers` | 库目录 |

标记层的保守取舍（自用版实跑出来的，别轻易放宽）：括号里出现字母/数字/汉字/
空白＝普通括号话，一律不碰（`（挑眉）`安全）；反引号里不算数——讨论这套语法
本身时不会当场喷贴纸；emoji 不在库/交集为空＝原样留在正文，坏掉的时候只是
一对普通括号，不穿帮；贴纸段发失败**不牵连已送达的文字段、也不自动重试**
（坑 17 的纪律，话已送到、脸没送到只记一笔）。

**孤儿贴纸防护**：脸是贴给它前面那句话的，所以**脸不许先于话出门**——标记
写在句首时贴纸先挂起，第一条正文真送达了才补发；正文发送中途抛错，挂起的脸
永不发送（一张没头没尾的表情比缺一张脸更糟，那是把语气安在一句不存在的话上）。
纯贴纸消息不受此限；贴纸自己发失败依旧不牵连正文。细节与理由见
[COOKBOOK「孤儿脸」一节](COOKBOOK.md)。

**多个 bot 共用一套库**：把两个 server 实例的 `TG_STICKER_DIR` 指到同一个目录
就行。馆藏（原图 + `file_unique_id` + 标签）天然共享；`file_id` 绑定 bot，
所以按 bot 分开缓存在 `file-ids.<bot_id>.json`，每个 bot 首次用某张时自动从
归档原图上传、把自己的 file_id 记在自己名下（懒迁移，不用手工逐张重传）。
只有 Telegram 明确回 400 才判 ID 失效；网络错/限流/5xx 都不会触发重复上传。

### 按钮问答：点一下就是答案（tg_ask_choice，实验性）

问对方选择题，不用等 ta 打字：题干+选项变成 inline keyboard，
**工具调用内同步等点击**，直接返回 `{"index": 1, "option": "B", "message_id": 123}`
——不用自己接回流、不用打插件补丁、零落盘。

> ⚠️ **实验性，如实相告**：按钮的显示布局是真机实测过的，但 getUpdates
> 轮询层目前只有单元测试背书——我们自家的 bot 被官方插件占着 getUpdates，
> 没条件跑实弹。用得顺或撞了怪事，都请开 issue 告诉我们。

```
tg_ask_choice(question="午饭吃什么？", options=["面", "饺子", "随便"])
```

布局规则（真机四组对照实测得来的）：

- 选项全部 **≤16 字（中文计，拉丁/数字按半字）** → 文字直接上按钮。
  每行几个自适应：全 ≤3 字一行 5 个（A-E 正好一排），≤8 字一行 2 个，
  再长一行 1 个；`columns` 显式给了听你的。
- **任何一条超线 → 整题自动切「正文列选项全文 + 1️⃣2️⃣3️⃣ 编号按钮」**。
  为什么这么狠：超长按钮文字会被 Telegram **像素级硬剪，连省略号都不给**——
  「先把测试跑绿然后再开始做」在按钮上会变成「先把测试跑绿然」，
  选项含义直接残废。显式 `layout="buttons"|"numbered"` 可以按住不切。
- 私聊只认聊天对面那个人的点击（别人点＝答 Not authorized，题继续等）；
  群里谁点都算，先到先得。
- 默认 `mark_answered=true`：选完原地收按钮、标上「✅ 已选」——**没人再轮询的
  按钮是幽灵按钮**，点了永远转圈。想自己控制选完的样子就传 `false`，
  之后用 `tg_rich_edit` 自己改。
- 超时（默认 600s，上限 3600）明确报错返回，题留在聊天里；超时的卡片
  同样会收按钮（`mark_answered=false` 时不收）。

#### 按钮问答要专用 bot

`getUpdates` 全 Telegram **同一时刻只允许一个消费者**。你的 bot 要是同时
挂着官方 telegram 插件、webhook、或另一个轮询进程，Telegram 回 409，
工具会带着这句话明确报错（不会傻等）。解法：去 @BotFather 给本 server
**单独造一只 bot**，token 写进 `~/.tg-rich-mcp.json`。

顺带的实话：ask 工具轮询期间会把这只 bot 的 `allowed_updates` 收窄到
`callback_query`（Telegram 会记住这个设置）——又一个别和其他消费者
共用 bot 的理由。

### 块速查（发送侧）

状态口径与 COOKBOOK 对齐：**✅ 本项目真发出去验过**／**📖 官方文档列出、没逐个实测**／
**⚠️ 有条件**。别把"文档列了"当成"实测过"。

**块级**

| type | 关键字段 | 状态 |
|---|---|---|
| `paragraph` | `text` | ✅ |
| `heading` | `text`, `size`(1-6，1 最大) | ✅ |
| `pre` | `text`, `language?` | ✅ |
| `footer` / `divider` | `text` / — | ✅ |
| `mathematical_expression` | `expression`（裸 LaTeX） | ✅ |
| `list` | `items[]`（每项 `blocks`，可加 `has_checkbox` / `is_checked`） | ✅ |
| `blockquote` | `blocks[]`, `credit?` | ✅ |
| `pullquote` | `text`, `credit?` | ✅ |
| `table` | `cells[][]`, `is_bordered?`, `is_striped?`, `caption?` | ✅ |
| `details` | `summary`, `blocks[]`, `is_open?` | ✅ |
| `anchor` | `name`（配行内 `anchor_link` 做页内跳转） | ✅ |
| `map` | `location{latitude,longitude}`, `zoom`, `width`, `height` | ✅ |
| `collage` / `slideshow` | `blocks[]`, `caption?`（caption 是**对象**不是字符串） | ✅ |
| `photo` | `photo`(InputMediaPhoto) + `caption?` | ✅ |
| `video` / `audio` / `animation` / `voice_note` | 对应 `InputMedia*` + `caption?` | 📖 未逐项实测 |
| `thinking` | `text` | ⚠️ **仅 draft 可用** |

**行内**：任何 `text` 字段都能传数组，元素是字符串或 `{type, text}`。
表格单元格的 `text` 同样收数组。

| type | 用处 | 注意 |
|---|---|---|
| `bold` `italic` `underline` `strikethrough` `code` | 基本样式 | |
| `spoiler` | 遮住，点开才看得见 | 答案、剧透 |
| `marked` | 高亮（荧光笔） | |
| `subscript` / `superscript` | 上下标 | 化学式、次方 |
| `mathematical_expression` | **行内公式** | 字段是 `expression`，不是 `text` |
| `url` | 带文字的链接 | 字段 `url` |
| `reference` | 脚注引用 | 配 `anchor` 块 |
| `anchor_link` | 页内跳转 | 字段是 `anchor_name`，不是 `name` |
| `date_time` | 按读者时区渲染 | 字段是 `unix_time` |
| `custom_emoji` | 自定义 emoji | 要 `custom_emoji_id` + `alternative_text` |

表格单元格：`text?`（省略＝不可见）、`is_header?`、`colspan?`、`rowspan?`、
`align`(left/center/right)、`valign`(top/middle/bottom)。

### 配方（给 agent 看的那部分）

这几条同时写进了工具的 `blocks` 参数描述里，不只写在 README。原因是这个包最初的教训：

> blocks 是原样透传的，上面这些块**从第一天起就能用**——但工具描述里没写，
> agent 就不会去试。对它来说，描述里没有的能力等于不存在。

所以描述里给的不是字段清单，是能直接套的形状：

```jsonc
// ① 句子里嵌公式，不用整块打断
{"type":"paragraph","text":["当 ",
  {"type":"mathematical_expression","expression":"x^2-5x+6=0"}," 时…"]}

// ② 答案遮住，点开才见（题卡、剧透）
{"type":"paragraph","text":["答案：",{"type":"spoiler","text":"B"}]}

// ③ 上下标
{"type":"paragraph","text":["H",{"type":"subscript","text":"2"},"O"]}

// ④ 折叠长内容（收起只占一行）
{"type":"details","summary":"展开看细节","blocks":[…]}

// ⑤ 长报告目录跳转
{"type":"anchor","name":"s1"}
{"type":"paragraph","text":[{"type":"anchor_link","text":"跳到第一节","anchor_name":"s1"}]}

// ⑥ 带勾选框的清单
{"type":"list","items":[{"has_checkbox":true,"is_checked":true,
  "blocks":[{"type":"paragraph","text":"做完了"}]}]}

// ⑦ 持久进度窗：send 一条 → 记住 message_id → 每帧 edit 它
```

**你自己加块类型时也照这个来**：往 schema 描述里塞一个能抄的形状，
比列十个字段名管用。

---

## 坑（比代码值钱的部分）

这些是真花时间试出来的，照着躲：

1. **版本别记错**：Rich Messages 首发在 **10.1**，不是 10.2。
   10.2 补的是**发送侧**的 `InputRichBlock*` 全族、`InputRichMessageMedia`、
   `InputMediaVoiceNote`，外加 Ephemeral Messages 和 Communities。
   到处流传的"10.2 支持富消息"不准确。

2. **`html` / `markdown` / `blocks` 三选一**，官方原文是 *Exactly one of the fields...*。
   混着给会被 API 拒收。本 server 在拼包前就拦下来了，报错比 API 的清楚。

3. 🔴 **草稿会锁死安卓用户的发送框——如今锁上配了钥匙，但钥匙在你手里**：
   `sendRichMessageDraft` 活跃期间，Telegram Android 把发送键换成省略号，
   用户**发不出任何消息**，**而且这期间在输入框里打的字，会在恢复时被清空**。
   官方缺陷记录 <https://bugs.telegram.org/c/62189> 被 Telegram 关闭称"预期行为"；
   后来 Bot API 10.3（2026-08-24）给出的解法是 `can_stop`：传 True 用户会看到
   一颗停止按钮，按下后草稿消失、发送框解锁（2026-09-04 Android 实测，客户端
   也要够新——旧版根本不画这颗按钮）。但三件事别想歪：
   - **按停后服务端照收你后续的帧**（实测一次没拒）。「用户喊停了」只通过
     `stopped_message_generation` Update 告诉你，bot 的收信侧不监听它＝瞎推到底。
     只开按钮不接事件是半套，别把停止按钮当承诺。
   - 停的是**这一个 draft**，不是"从此自由打字"——Telegram 的模型仍是
     Stop → 再输入，不是边生成边聊。
   - 客户端会把已停 draft_id 的后续帧直接扔掉（实测），锁不会复活。
   ⇒ 长任务进度窗**默认仍用 `tg_rich_send` + `tg_rich_edit`**（不锁、可回看）；
   确要草稿的动画质感，`can_stop: true` 是底线配置，且收信侧得真处理 stop 事件。
   （桌面端据用户反馈不锁——用户反馈，不是官方的跨平台保证。）

4. **草稿另有三条硬约束**：只能发**私聊**、只活 **30 秒**、**不进聊天记录**，
   定稿后必须再发一条正式消息才留得住。

5. 🔴 **复制会塌，转发不会**（用户实测）：结构是客户端渲染的，剪贴板只接得住
   **纯文本那一层**，粘出来是塌平的一长条（表格变行、折叠块摊开、公式变裸 LaTeX、
   spoiler 直接露馅）；**转发**则渲染完整。
   还有一条：用户**转发富消息给 bot 时，agent 读到的也只是纯文本**——
   人看得见结构，机器看不见。
   ⇒ 要被复制走的东西别包富格式（命令、代码、要转贴的段落用普通消息或代码块）；
   给第三方看效果用转发或截图；**别把富消息当机器间的数据通道**。

6. **`draft_id` 必须非零**，且同一个 id 的连续调用才会做动画过渡。
   每帧换 id ＝ 每帧一个新窗口，看着就是在闪。

7. **`thinking` 块只在 draft 里能用**，正式消息里给了会被拒。

8. **有几个字段名不是你以为的那个**（错了 API 会明确告诉你缺哪个字段，但先知道省一轮）：
   行内公式是 `expression` 不是 `text`；`anchor_link` 用 `anchor_name` 不是 `name`；
   `date_time` 用 `unix_time`；`custom_emoji` 还要 `alternative_text`；
   `collage`/`slideshow` 的 `caption` 是对象不是字符串。

9. **手写 blocks 很啰嗦**：一个像样的表格 ~60 行 JSON。
   ⇒ 日常走 `markdown`，表格/公式才上 `blocks`。

10. **抓官方 changelog 要加超时和压缩**：
   `core.telegram.org/bots/api-changelog` 页面约 840KB，
   `curl` 默认 30 秒会超时 —— 用 `curl -s --compressed --max-time 120`。

11. **别往 stdout 打字**。MCP 走 JSON-RPC over stdio，server 里多 print 一行就把协议冲了。
   调试信息写 stderr。（这就是为什么本 server 不复用任何会 print 的现成函数。）

12. **token 会藏在异常消息里**。`requests` 的连接异常经常把整条 URL（含 token）塞进消息。
    所有对外返回的错误都在出口统一过一遍脱敏——**别只包网络那一段**：
    `int("非数字")` 这类校验异常同样会把原值带出去，而调用方可能刚好把 token 填错了位置。
    （写这个包的当晚，作者自己在临时测试脚本里就漏了一次。加个函数不等于安全，
    每条出口都得过。）

13. **hook 拿不到 MCP server 的 env**（见「装 · 1」）。

14. **并发工具调用会互相盖**。Claude Code 会并行发一批工具调用，
    N 个 hook 进程同时读改写同一个状态文件——实测 100 个并发只剩 24 条。
    本包用 `flock` + 原子替换 + 帧序号解决；你自己改的话别把这三样拆开。

15. **`session_id` 是外来输入**，直接拼进文件路径能用 `../` 跑出目录。本包只用它的哈希。

16. **要上传本地文件的话用定长 multipart**（`requests` 的 `files=`），别用流式。
    症状签名：**文本消息一直正常、发文件稳定报网络错**——先别怀疑 Telegram，
    大概率是你的 HTTP 客户端在用流式 body 发 multipart、被代理掐了
    （文本是定长 JSON POST，不受影响，所以只有媒体炸）。
    把文件整读进内存再发（定长 body）就好。⚠️ "整读是否安全"取决于**进程可用内存**：
    本工具按每文件 50 MiB / 累计默认 200 MiB 拦（拼 multipart 还有额外开销，未测实际
    RSS），别把"≤50MB"读成"任意情况下整读都无害"。真要发超大批量该走临时文件流式
    ——"定长"不要求所有内容都驻留内存。文件字节预算、请求体、进程内存是三码事。

17. **组合发送半途失败 ⇒ 重复消息**：先发文字段、再发文件段（或多个文字段）的
    复合调用，后段炸的时候前段其实已经送达；调用方看到整体报错去重试整条，
    用户就收到两遍。复合发送要**分段记账**，重试只补真正失败的那一段。
    本工具的做法（`tg_rich_send` 的句内贴纸分段路径）：无论成败都返回一份
    **机器可读的分段送达账**，附在人类文案后的 `分段送达账（机器可读）：` 之后，
    是一个 `{"delivered":[…],"failed":[…],"unknown":[…]}` 的 JSON——
    - `delivered`：已确认送达的段，文字段带 `message_id`；
    - `failed`：服务器**明确拒收**（`ok:false`）的段，肯定没发出去，可安全补这一段；
    - `unknown`：**超时/网络错**的段，可能已被 Telegram 收到，**送达状态未知**。
    后段失败时前段的 `delivered`（含 message_id）**一定保留在错误结果里**，
    整条被标 `isError` 但绝不自动整条重发。分不清「没发出去」和「发了没拿到回执」
    时（即 `unknown`）宁可不补发（同「渲染器模式」那条降级护栏，一个道理）——
    要补也只补 `unknown`/`failed` 里点名的那几段，别把 `delivered` 的再发一遍。

---

## 进度窗的三条铁律

改 `tg_progress_hook.py` 时别破这三条，它们是它能常驻在每次工具调用前面的原因：

1. **永不阻断工具调用**——任何异常都吞掉，永远 `exit 0`。
   推送失败最多让窗口不动，绝不能让 agent 干不了活。
2. **不拖慢工具调用**——网络请求丢给 detached 子进程，主体只写状态就退。
   hook 串在每次调用前面，多花的每毫秒都乘以调用次数。
   （实测主体 ~0.03s；这是正常输入 + 本地文件系统下的数字，不是绝对保证。）
3. **不泄密**——见上面「安全闸」。可以关，但要知道自己在关什么。

---

## 它不能做什么

- **渲染搬不走**。表格和公式是 Telegram 客户端渲染的，你自己的前端拿不到这个能力。
  能复用的是**块协议**：让 agent 统一产出 block JSON，TG 端原样丢给 API，
  自家前端另写渲染器 → 一份输出两个消费端。
- **进度窗不是逐字流式**。它推的是**工具级事件**（要调工具了），
  不是 agent 一个字一个字打出来的过程——大多数 agent 的文本是整块产出的，
  系统里根本不存在"半句话"。想要逐字，得从模型的流式输出那一层接，不是这里。

## 还没做的

- **手机权限审批卡 `tg_ask_permission`（设计定稿、参考实现进过一版、主动下架）**：
  把 Claude Code 的权限对话框搬到手机——`claude -p … --permission-prompt-tool
  mcp__tg-rich__tg_ask_permission`，TG 弹「⚙️ Claude 想调用：Bash + 参数摘要 +
  ✅允许/❌拒绝」，返回官方权限契约 JSON。参考实现（含 12 条契约测试）在
  git 历史 `5b0c548`，下架原因很朴素：**我们自家的 bot 被官方插件占着
  getUpdates，这个工具我们自己一次实弹都没跑过——没验证过稳定性的东西，
  不该顶着「能用」的样子上架**。权限决策错一次的代价比选择题高一个量级。
  谁有专用 bot 想把它接回来，设计红线有四条，一条都别省：
  ① **超时＝拒绝（fail-closed）**，且必须是正常返回的 deny 契约，不能抛异常
  （抛了 Claude Code 拿不到 JSON，行为不可预期）；② 参数摘要**逐行**过密钥闸
  （复用 `tg_progress_hook` 的 DIRTY/SECRET_SHAPES，整块全遮＝看不见参数的
  审批等于抛硬币）；③ **只走私聊**——群里谁都能点「允许」，那不是审批是抽奖；
  ④ 契约字段名一个都不能错（`behavior`/`updatedInput`/`message`）。
  另有交互窗路线：flag 只管 non-interactive，交互窗走 PreToolUse hook 返回
  `permissionDecision`，机制层同样全复用。**两条路线都等实弹验证后再上架**。
- **媒体 file_id 自动复用**：发过的本地文件按**内容哈希**记 file_id
  （每 bot 一份），重发同一个文件自动免上传、agent 无感。设计要点：
  上传后要把「哪个文件 → 哪个 file_id」学下来，多媒体消息的对应关系
  得拿输入 blocks 的 attach 顺序对响应的媒体顺序，**数量对不齐就放弃学习**
  ——宁可下次重传，不能学错一张图。谁要用得上，欢迎按这个写。
- **Ephemeral Messages**（10.2 起；**10.3（2026-08-24）改为 `ephemeral_message_parameters`
  对象**）：群里只有指定用户和 bot 能看见的消息，可编辑可删。10.3 起
  `sendRichMessage` 也带上了这个参数——即**Telegram 已支持富消息发群内悄悄话**，
  只是**本工具尚未暴露**这个参数。值得单独一个工具。
  见[官方 10.3 更新](https://core.telegram.org/bots/api#august-24-2026)。
- `InputRichMessage.media`：markdown / html 模式里引用媒体。
- 状态文件不清理（一个会话一个 json，量极小）。

---

*MIT。写完之后请两位独立审查者各审了一遍，找出的问题都在上面的坑和代码注释里。*
