# Telegram 富消息能玩什么 —— 能力全景与配方

> 配套 `tg-rich-mcp`。Bot API **10.1**（2026-06-11）首发 Rich Messages，
> **10.2**（07-14）补齐发送侧 `InputRichBlock*` / `InputRichMessage.blocks` /
> `InputRichMessage.media` / `InputMediaVoiceNote`。
>
> 这份是**清单式**的：能做的都列出来，包括你大概率用不上的（地图、轮播、银行卡号实体）。
> 列出来的目的不是让你都用，是让你**知道有这条路**——不知道的能力等于不存在。
>
> 标 ✅ 的是本项目真发出去验过的；标 📖 的是官方文档列出、本项目没逐个实测的。

---

## 0. 先分清两件事

很多人一上来就把「富消息」和「流式」绑在一起想，然后被 30 秒草稿和安卓的锁输入框
按在地上。它们是**两个维度**：

| 维度 | 选项 |
|---|---|
| **内容怎么写** | `markdown` / `html` / `blocks` —— **三选一**，不能混 |
| **消息怎么活** | `send`（正式） / `edit`（原地改） / `draft`（30 秒草稿） / `delete`（撤回） |

blocks 完全可以用在正式消息和原地编辑上。要不要流式，跟你用不用 blocks 无关。

### 生命周期速查

| 想干的事 | 方法 | 进聊天记录 | 备注 |
|---|---|---|---|
| 发一条 | `sendRichMessage` | 是 | 最终回复、报告、题卡 |
| 原地改 | `editMessageText` + `rich_message` | 改原消息 | 持久进度窗、仪表盘；**不响铃** |
| 撤掉 | `deleteMessage` | 消失 | bot 只能删 48 小时内自己发的 |
| 推草稿 | `sendRichMessageDraft` | 否，约 30 秒 | 动画预览；🔴 见下 |

> 🔴 **草稿会锁死安卓用户的发送框**：`sendRichMessageDraft` 活跃期间，
> Telegram Android 把发送键换成省略号，用户发不出消息，**这期间在输入框里
> 打的字会在恢复时被清空**。官方缺陷记录 <https://bugs.telegram.org/c/62189>
> 已被关闭，称是"当前预期行为"。
> 长任务里用户最想插话的时刻恰好是草稿最活跃的时刻 ⇒ **进度窗用 send + edit**，
> 草稿留给短动画、且只在你确认用户不在安卓上时。

### 三种写法怎么选

| 写法 | 好在哪 | 代价 | 什么时候用 |
|---|---|---|---|
| `markdown` | agent 最容易生成，接近 GFM | 高级结构控制不细 | 日常回答、笔记、审查摘要 |
| `html` | 模板化方便，高级标签直观 | 转义和嵌套容易错 | 固定报告模板、媒体排版 |
| `blocks` | 字段明确、可校验、能被别的前端复用 | JSON 啰嗦（一个表格 ~60 行） | 表格、进度窗、复杂媒体 |

---

## 1. 行内样式（放在任何 `text` 里）

### Markdown 写法

```markdown
**粗体**    __粗体__
*斜体*      _斜体_
~~删除线~~
`行内代码`
==高亮==            ← 荧光笔
||剧透，点开才见||

[普通链接](https://example.com)
[邮箱](mailto:a@b.com)
[电话](tel:+123456789)
[@某个用户](tg://user?id=123456789)

![自定义 Emoji](tg://emoji?id=5368324170671202286)
![按读者时区显示的时间](tg://time?unix=1787673600&format=wDT)

$x^2+y^2$           ← 行内公式
```

Markdown 没有直接语法的，混 HTML 就行：

```html
<u>下划线</u>  <ins>下划线</ins>
H<sub>2</sub>O   x<sup>2</sup>
<tg-spoiler>剧透</tg-spoiler>
```

### blocks 写法（`text` 传数组）

任何收 `RichText` 的 `text` 字段，都可以传**字符串**或**字符串与对象混合的数组**。

| `type` | 特殊字段 | 用处 | 状态 |
|---|---|---|---|
| `bold` `italic` `underline` `strikethrough` | `text` | 基本样式 | ✅ |
| `code` | `text` | 行内代码 | ✅ |
| `spoiler` | `text` | 遮住，点开才见 | ✅ |
| `marked` | `text` | 高亮 | ✅ |
| `subscript` / `superscript` | `text` | 上下标 | ✅ |
| `mathematical_expression` | **`expression`** | 行内公式 | ✅ 字段不是 `text` |
| `url` | `text`, `url` | 带文字的链接 | ✅ |
| `email_address` | `text`, `email_address` | 邮箱实体 | 📖 |
| `phone_number` | `text`, `phone_number` | 电话实体 | 📖 |
| `text_mention` | `text`, `user` | 指定用户（无 @） | 📖 |
| `mention` | `text`, `username` | `@username` | 📖 |
| `hashtag` / `cashtag` | `text`, `hashtag`/`cashtag` | `#话题` / `$USD` | 📖 |
| `bot_command` | `text`, `bot_command` | `/命令` | 📖 |
| `bank_card_number` | `text`, `bank_card_number` | 卡号实体 | 📖 |
| `date_time` | `text`, **`unix_time`**, `date_time_format` | 按**读者时区**渲染 | ✅ |
| `custom_emoji` | `custom_emoji_id`, `alternative_text` | 自定义 emoji | ✅ |
| `anchor` | `name` | 页内锚点 | ✅ |
| `anchor_link` | `text`, **`anchor_name`** | 页内跳转 | ✅ |
| `reference` | `text`, `name` | 定义脚注 | ✅ |
| `reference_link` | `text`, `reference_name` | 引用脚注 | ✅ |

> `date_time` 值得单说：你给 unix 时间戳，**每个读者看到的是自己时区的时间**。
> 跨时区约时间不用再写"北京时间"了。

---

## 2. 块级结构

### Markdown

````markdown
# 一到六级标题
## 二级

普通段落。

```python
print("带语言标记的代码块")
```

---                        ← 分隔线

- 无序    * 无序    + 无序
1. 有序
- [ ] 没做完
- [x] 做完了

> 引用第一段
>
> 引用第二段

| 项目 | 状态 | 耗时 |
|:--|:--:|--:|
| 单元测试 | 通过 | 12s |

这句有脚注[^a]。

[^a]: 脚注正文，收在正文之外。

$$
\sum_{i=1}^{n}x_i
$$
````

块级公式也可以写成 ` ```math ` 代码块。

折叠块（HTML，Markdown 里直接混）：

````html
<details>
<summary>展开查看日志</summary>

### 里面照样能写 Markdown

- 日志一
- 日志二

</details>

<details open><summary>默认展开</summary>内容</details>
````

居中强调引语：

```html
<aside>最重要的一句话<cite>署名</cite></aside>
```

### blocks 总表

| `type` | 关键字段 | 用处 | 状态 |
|---|---|---|---|
| `paragraph` | `text` | 段落 | ✅ |
| `heading` | `text`, `size` 1–6（1 最大） | 标题 | ✅ |
| `pre` | `text`, `language?` | 代码块 | ✅ |
| `footer` | `text` | 页脚/弱化说明 | ✅ |
| `divider` | — | 分隔线 | ✅ |
| `mathematical_expression` | `expression`（裸 LaTeX，别包 `$$`） | 块级公式 | ✅ |
| `anchor` | `name` | 页内锚点 | ✅ |
| `list` | `items[]`，每项 `blocks`，可加 `has_checkbox` / `is_checked` | 三种列表 | ✅ |
| `blockquote` | `blocks[]`, `credit?` | 引用，可署名 | ✅ |
| `pullquote` | `text`, `credit?` | 居中强调引语 | ✅ |
| `table` | `cells[][]`, `is_bordered?`, `is_striped?`, `caption?` | 原生表格 | ✅ |
| `details` | `summary`, `blocks[]`, `is_open?` | 折叠 | ✅ |
| `map` | `location`, `zoom`, `width`, `height`, `caption?` | 地图 | 📖 |
| `collage` | `blocks[]`, `caption?` | 多图拼贴 | 📖 |
| `slideshow` | `blocks[]`, `caption?` | 轮播 | 📖 |
| `photo` `video` `animation` `audio` `voice_note` | 对应媒体 + `caption?` | 媒体块 | 📖 |
| `thinking` | `text` | 思考占位 | ⚠️ **只有 draft 能用** |

表格单元格字段：`text?`（省略＝该格不可见）、`is_header?`、`colspan?`、`rowspan?`、
`align`(left/center/right)、`valign`(top/middle/bottom)。单元格的 `text` 同样收
RichText 数组，所以格子里能放粗体、链接、代码、高亮。

---

## 3. HTML 标签全表

行内：`<b> <strong> <i> <em> <u> <ins> <s> <strike> <del> <code> <mark>
<sub> <sup> <tg-spoiler>`

链接与跳转：

```html
<a href="https://example.com">外链</a>
<a href="mailto:a@b.com">邮箱</a>   <a href="tel:+123">电话</a>
<a href="tg://user?id=123">TG 用户</a>

<a href="#chapter-1">跳到第一节</a>   <a name="chapter-1"></a>
<a href="#note-1">脚注引用</a>       <tg-reference name="note-1">脚注正文</tg-reference>
```

特殊实体：

```html
<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>
<tg-time unix="1787673600" format="wDT">兜底文字</tg-time>
<tg-math>x^2+y^2</tg-math>
```

块级：`<h1>…<h6> <p> <pre><code class="language-python"> <footer> <hr/>
<ul><li> <ol><li> <blockquote><cite> <aside><cite> <details><summary>`

有序列表还能设 `start` / `type` / `reversed`，单项设 `value`；
任务清单用 `<input type="checkbox">`。

媒体与地图：

```html
<tg-map lat="1.2868" long="103.8545" zoom="13"/>

<figure>
  <img src="https://example.com/p.jpg" tg-spoiler/>   ← 图片也能打码
  <figcaption>说明<cite>来源</cite></figcaption>
</figure>

<video src="…mp4"></video>   <audio src="…mp3"></audio>

<tg-collage><img src="1.jpg"/><img src="2.jpg"/></tg-collage>
<tg-slideshow><img src="1.jpg"/><video src="2.mp4"/></tg-slideshow>
```

仅草稿可用：`<tg-thinking>正在处理…</tg-thinking>`

---

## 4. 媒体怎么带

两条路：

1. **外部 URL**（只支持 HTTP/HTTPS）：`![](https://example.com/photo.jpg)`，
   媒体必须**单独成块**，不能嵌在段落里。
2. **上传/复用**：走 `InputRichMessage.media`，然后在正文里用
   `tg://photo?id=…` / `tg://video?id=…` / `tg://audio?id=…` 引用。

上传本地文件用**定长 multipart**（`requests` 的 `files=`），别用流式——过代理容易崩。

---

## 5. 配方（能直接抄的形状）

### 句中嵌公式

```json
{"type": "paragraph", "text": [
  "当 ", {"type": "mathematical_expression", "expression": "x^2-5x+6=0"}, " 时，继续求根。"
]}
```

### 答案遮住的题卡

```json
[
  {"type": "heading", "size": 3, "text": "第 12 题"},
  {"type": "paragraph", "text": "下列哪项最能削弱上述论证？"},
  {"type": "paragraph", "text": ["答案：", {"type": "spoiler", "text": "B"}]},
  {"type": "details", "summary": "解析", "blocks": [
    {"type": "paragraph", "text": "题干的因果链在……"}
  ]}
]
```

自己做题时答案默认遮住，做完点开——比另发一条消息干净。

### 任务清单

```json
{"type": "list", "items": [
  {"has_checkbox": true, "is_checked": true,
   "blocks": [{"type": "paragraph", "text": "单元测试"}]},
  {"has_checkbox": true,
   "blocks": [{"type": "paragraph", "text": "真机验收"}]}
]}
```

### 原生表格（带斑马纹）

```json
{"type": "table", "is_bordered": true, "is_striped": true, "caption": "验证结果",
 "cells": [
   [{"text": "检查", "is_header": true}, {"text": "状态", "is_header": true}],
   [{"text": "单元测试"}, {"text": [{"type": "marked", "text": "通过"}]}]
 ]}
```

### 长报告目录跳转

```json
[
  {"type": "paragraph", "text": [
    {"type": "anchor_link", "text": "跳到风险项", "anchor_name": "risks"}]},
  {"type": "anchor", "name": "risks"},
  {"type": "heading", "size": 2, "text": "风险项"}
]
```

### 持久进度窗（长任务的正确姿势）

```text
sendRichMessage  → 记住 message_id
  ├ heading   正在执行
  ├ list      最近 N 个步骤
  ├ table     测试状态
  └ footer    累计步数 / 用时

每次事件 → editMessageText(message_id, rich_message)
收工     → 要么改成终态，要么 deleteMessage 撤掉
```

**别用草稿代替这个流程**，尤其当用户可能在安卓上。

---

## 6. 几种消息的组合套路

| 场景 | 怎么搭 |
|---|---|
| **代码审查报告** | heading 结论 → paragraph 一句话风险 → table（文件/严重度/问题）→ details 每条的证据 → list 修复顺序 → footer 验证边界 |
| **运维/测试报告** | table（服务/状态/延迟）→ `marked` 标异常值 → details 原始日志 → pre 复现命令 → footer 采样时间 |
| **学习题卡** | heading 题目 → paragraph 题干 → spoiler 答案 → details 分步解析 → 公式 → 任务清单做错因复盘 |
| **多媒体交付** | collage 多图并排 / slideshow 步骤截图 / caption + credit / 敏感图上 spoiler 遮罩 / voice_note 语音说明 / map 位置 |
| **长任务进度** | 见上面的持久进度窗 |

---

## 7. 官方限制（撞上了不要怀疑自己）

- 文本最多 **32,768** 个 UTF-8 字符（含自定义 emoji 的 fallback 和公式源码）。
- 最多 **500** 个 block、**16** 层嵌套、**50** 个媒体附件。
- 表格最多 **20** 列。
- `markdown` / `html` / `blocks` **三选一**（官方原文 *Exactly one of the fields*）。
- Markdown 表格的单元格**只允许行内格式**（要 colspan/rowspan/边框就上 blocks）。
- 媒体块必须**单独成块**。
- 外部 URL 媒体只支持 HTTP/HTTPS。
- `thinking` 块**只能用于草稿**。
- 草稿：**私聊限定**、约 **30 秒**、不进聊天记录、`draft_id` 必须非零且连续调用同一个
  id 才有动画、定稿必须补发正式消息；当前 Bot API **没有** `deleteRichMessageDraft`。

### 🔴 复制会塌，转发不会（用户实测）

| 怎么搬 | 结果 |
|---|---|
| **复制粘贴** | ❌ 塌成一长条。表格变行、折叠块全摊开、公式变裸 LaTeX、spoiler 直接露馅 |
| **转发** | ✅ 渲染完整，跟原消息一样 |
| **转发给 agent** | ⚠️ 人看得见，**agent 读不到富结构**——它拿到的只有纯文本层 |

原因是同一个：表格、折叠块、公式、spoiler 都是**客户端渲染**的，消息传的是结构描述。
系统剪贴板只接得住纯文本那一层；转发是把整条消息原样递过去，所以结构还在。

⇒ 三条实践规矩：

1. **要被复制走的东西别包富格式**：给用户的命令、代码、要转贴的段落，
   用普通消息或代码块更实在。
2. 想让第三方看到效果，用**转发**或截图，别让用户复制粘贴。
3. **别把富消息当机器间的数据通道**：用户转发一条富消息给 bot，bot 读到的是塌平的
   纯文本。要给 agent 传结构化数据，走 API / 文件 / 你自己的协议。

这跟「渲染搬不走」（你自己的前端拿不到 Telegram 的渲染能力）是同一件事的三面：
能复用的永远是**块协议**，不是渲染结果。

## 8. 和富消息相邻、但不是同一套的东西

- **Ephemeral Messages**（10.2）：群里发只有指定用户和 bot 看得见的消息
  （`receiver_user_id`），可编辑可删，还有 `BotCommand.is_ephemeral`
  ——命令的响应只对发起人可见。
  ⚠️ 官方列出的接收方法**不包含** `sendRichMessage`，别把"富消息"和"群内悄悄话"
  当成同一个接口能力。
- `message_effect_id`（私聊限定的消息特效）、`protect_content`、
  `skip_entity_detection`（关掉 URL/邮箱/命令的自动识别）、
  `message_thread_id`、business connection、suggested post、付费广播参数。

---

## 9. 官方资料

- Bot API changelog：<https://core.telegram.org/bots/api-changelog>
- Rich Message Formatting：<https://core.telegram.org/bots/api#rich-message-formatting-options>
- `InputRichMessage`：<https://core.telegram.org/bots/api#inputrichmessage>
- `sendRichMessage`：<https://core.telegram.org/bots/api#sendrichmessage>
- `sendRichMessageDraft`：<https://core.telegram.org/bots/api#sendrichmessagedraft>
- 安卓草稿锁输入框：<https://bugs.telegram.org/c/62189>

---

*本文的能力盘点由一份独立调研整理而来（对照 10.1/10.2 changelog、Rich Message
文档与官方缺陷记录）；✅ 的部分是本项目真发出去验过的，📖 的没有逐个实测——
照抄前自己发一条试试，比信文档快。*
