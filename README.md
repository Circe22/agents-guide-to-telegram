# tg-rich-mcp

**让 agent 在 Telegram 里发原生表格、LaTeX 公式、折叠块——以及让你在手机上看着它干活。**

> <https://github.com/Circe22/tg-rich-mcp> · MIT · 只依赖 `requests`
> 发现 bug 或者 Telegram 又更新了，欢迎开 issue / 提 PR。

Telegram 在 Bot API **10.1**（2026-06-11）加了 Rich Messages，10.2（07-14）补齐发送侧。
官方 telegram 插件的 `reply` 够不着这些，这个包直投 Bot API 把它接进来。

两件东西，可以只用一件：

| 文件 | 是什么 | 通用性 |
|---|---|---|
| `tg_rich_mcp.py` | MCP server，三个工具：发 / 原地改 / 推草稿 | ✅ 走**握手式** MCP 的 host（Claude Code、Claude Desktop、Cursor、自己写的 agent）——协议版本见下 |
| `tg_progress_hook.py` | 进度窗 hook：每次调工具前推一行 | ⚠️ **仅 Claude Code**（靠它的 PreToolUse 钩子，别的 host 没有这个机制），且需要 Linux / macOS / WSL |
| `secret_redaction.py` | 密钥形态的单一真源，上面两个都用它 | 跟着走，别单独删 |
| `test_tg_rich_mcp.py` | 55 个测试，`python3 -m unittest discover -v`，1 秒内、不发网络 | — |

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

> **TL;DR (English)** — An MCP server exposing Telegram's Rich Message API
> (native tables, LaTeX, collapsible blocks, in-place edits, streaming drafts)
> to any handshake-based stdio MCP host (protocol 2024-11-05 … 2025-11-25),
> plus a Claude Code hook that streams your agent's tool calls
> into a live Telegram window. Config via `~/.tg-rich-mcp.json`.
> Only dependency: `requests`. Redaction is on by default — `TG_PROGRESS_REDACT=0` disables it.
>
> ⚠️ **Android caveat**: while a streaming draft is active, Telegram Android replaces the
> user's send button with an ellipsis — they cannot send anything, and text already typed
> gets wiped when the input recovers ([bugs.telegram.org/c/62189](https://bugs.telegram.org/c/62189),
> closed by Telegram as expected behaviour). The progress hook therefore defaults to
> `sendRichMessage` + `editMessageText` and deletes the window when done;
> drafts are opt-in via `TG_PROGRESS_MODE=draft`.

---

## 装

### 1. 配置

```bash
cat > ~/.tg-rich-mcp.json <<'JSON'
{
  "bot_token": "123456:AA...",
  "chat_id": "你的 chat id",
  "proxy": "http://127.0.0.1:7897"
}
JSON
chmod 600 ~/.tg-rich-mcp.json
```

`proxy` 不需要就删掉那行。也支持环境变量 `TG_BOT_TOKEN` / `TG_CHAT_ID` / `TG_PROXY`（优先级更高）。
配置在进程启动时读一次并缓存，**改了要重启 MCP server 才生效**。

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

只依赖 `requests`（`pip install requests`）。协议是手写的 JSON-RPC over stdio，不需要 mcp SDK。

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
> 好在它留了向后兼容的路：**同时支持新旧两代协议的 stdio 客户端**，会先拿
> `server/discover` 探测、失败再按旧握手回退——本 server 对它回 `method not found`，
> 回退即成立（实测就是这个响应）。但**只实现了 2026-07-28 的客户端不在此列**，
> 它连不上这个 server，这不是 bug，是两代协议的分界。
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

#### 它默认怎么干活

**发一条正式消息，然后每帧原地改它**（`sendRichMessage` → `editMessageText`），
收工时把这条消息**撤掉**——聊天记录里一条工具调用都不留。

> ⚠️ **为什么默认不是流式草稿**：`sendRichMessageDraft` 活跃期间，
> **Telegram Android 会把用户的发送键换成省略号，用户发不出消息，
> 而且这期间在输入框里打的字会在恢复时被清空。**
> 官方缺陷记录 <https://bugs.telegram.org/c/62189> 已被关闭，称是"当前预期行为"
> ——不是等一个修复就能好的事。
>
> 长任务里用户最需要插话的时刻（补条件、喊停、纠方向、回答 agent 的提问），
> 恰好就是草稿最活跃的时刻。所以草稿只在你显式打开时才走。
> 桌面端据用户反馈不锁输入框——那是**用户反馈，不是官方的跨平台保证**。

| 想要什么 | 怎么设 |
|---|---|
| 默认（持久窗 + 收工撤掉） | 什么都不用设 |
| 干完把窗口留下来当记录 | `TG_PROGRESS_END=keep` |
| 就要那种流式动画（**接受安卓锁输入框**） | `TG_PROGRESS_MODE=draft` |
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

两条护栏（这个接法实跑出来的，别省）：

1. **降级只认 400 / 404**：Telegram 明确拒收（格式/能力问题）时才退回普通
   `sendMessage` 重发同一段；**网络错、超时、5xx、429 一律原样抛错，不自动补发**——
   这些状态下 Telegram 可能已经收到了第一条，自动补发＝制造重复消息。
2. **只包纯文字出口**：引用回复、附件、贴纸等旁路照走原来的路，别把整条发送链
   都塞进渲染器——包的面越大，降级时要复原的状态越多。

### 长任务：持久进度窗（推荐）

```
tg_rich_send(blocks=[...])        → 返回 message_id
tg_rich_edit(message_id=…, blocks=[...])   ← 每帧原地改
```

`editMessageText` 收 `rich_message`（10.1 加的），所以进度窗**不必**用 30 秒草稿：
发一条正式消息、之后原地编辑，留在聊天记录里、编辑还不响铃。

`tg_rich_draft` 只在你要那种 30 秒动画质感时才用（私聊限定，不进聊天记录，
定稿必须补一条正式消息）。**它会锁住安卓用户的发送框**，别拿它当长任务的默认进度方案
（见上面的挂 hook 那节）。

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
  单个 `photo` 块就是普通发图。每个文件 ≤50MB、一条消息最多 50 个。
- 发送成功的返回里带每个媒体的 **file_id**。存下来，下次 `media` 直接填
  file_id 复用，不用重新上传。**复用时整串程序化取用，别看着截断的显示手补
  尾巴**——file_id 彼此长得几乎一样，手打命中纯靠运气（作者试过，侥幸没炸）。
- 文件名形态闸：`.env` / `id_rsa` / `*.pem` / 名字含 token·credential·secret
  之类的文件会被拒，符号链接按**真实目标**检查。会误伤 `my_secret_santa.jpg`
  这种名字——确认无害就设 `TG_RICH_MEDIA_GUARD=0`。

### 块速查（全部实测发得出去）

**块级**

| type | 关键字段 |
|---|---|
| `paragraph` | `text` |
| `heading` | `text`, `size`(1-6，1 最大) |
| `pre` | `text`, `language?` |
| `footer` / `divider` | `text` / — |
| `mathematical_expression` | `expression`（裸 LaTeX） |
| `list` | `items[]`（每项 `blocks`，可加 `has_checkbox` / `is_checked`） |
| `blockquote` | `blocks[]`, `credit?` |
| `pullquote` | `text`, `credit?` |
| `table` | `cells[][]`, `is_bordered?`, `is_striped?`, `caption?` |
| `details` | `summary`, `blocks[]`, `is_open?` |
| `anchor` | `name`（配行内 `anchor_link` 做页内跳转） |
| `map` | `location{latitude,longitude}`, `zoom`, `width`, `height` |
| `collage` / `slideshow` | `blocks[]`, `caption?`（caption 是**对象**不是字符串） |
| `photo`/`video`/`audio`/`animation`/`voice_note` | 对应 `InputMedia*` + `caption?` |
| `thinking` | `text` —— **仅 draft 可用** |

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

3. 🔴 **草稿会锁死安卓用户的发送框**（这条是全表最贵的一条，用户实机撞出来的）：
   `sendRichMessageDraft` 活跃期间，Telegram Android 把发送键换成省略号，
   用户**发不出任何消息**，**而且这期间在输入框里打的字，会在恢复时被清空**。
   官方缺陷记录 <https://bugs.telegram.org/c/62189> 已被 Telegram 关闭，
   称是"当前预期行为"——不是等一个修复就能好的事。
   ⇒ 长任务进度窗**别用草稿**：agent 干得越久，用户越可能想插话（补条件、喊停、
   纠方向），而那正好是他被锁住的时候。用 `tg_rich_send` + `tg_rich_edit`。
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
    把文件整读进内存再发（定长 body）就好，≤50MB 的上限内整读是安全的。

17. **组合发送半途失败 ⇒ 重复消息**：先发文字段、再发文件段的复合调用，
    文件段炸的时候文字其实已经送达；调用方看到整体报错去重试整条，
    用户就收到两遍文字。复合发送要**分段记账**，重试只补真正失败的那一段；
    分不清「没发出去」和「发出去了但没拿到回执」时，宁可不补发
    （同「渲染器模式」那条降级护栏，一个道理）。

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

- **Ephemeral Messages**（10.2）：群里只有指定用户和 bot 能看见的消息
  （`receiver_user_id`，`sendMessage` 全族都收，可编辑可删）。
  值得单独一个工具——群里给某个人发悄悄话，别人看不见。
- `InputRichMessage.media`：markdown / html 模式里引用媒体。
- 状态文件不清理（一个会话一个 json，量极小）。

---

*MIT。写完之后请两位独立审查者各审了一遍，找出的问题都在上面的坑和代码注释里。*
