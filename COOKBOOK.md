# Telegram 富消息能玩什么 —— 能力全景与配方

> 配套 `agents-guide-to-telegram`（前名 tg-rich-mcp）。Bot API **10.1**（2026-06-11）首发 Rich Messages，
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
>
> Bot API 10.3（2026-08-24）起有条件放宽：`can_stop: true` 给用户一颗停止按钮，
> 按下后草稿消失、发送框解锁（Android 实测，客户端要够新）。但**服务端照收你
> 按停之后的帧**——bot 想知道用户喊停，必须让收信侧监听
> `stopped_message_generation` Update（README 坑 3 有完整账）。只开按钮不接
> 事件＝半套；接不了事件的场合，上面那条「进度窗用 send + edit」照旧成立。

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

> **折叠有两种做法，各有各的短板**（真人长期用出来的，不是读文档读出来的）：
>
> | 做法 | 收起时 | 怎么收回 |
> |---|---|---|
> | `details` 块 | ✅ 只占一行，最干净 | ❌ 长内容展开后**得滚回顶部**点那根顶栏；在消息中间点哪儿都收不回来 |
> | 可展开引用块（`<blockquote expandable>`，普通消息就有） | ❌ 仍露着前几行，占地方 | ✅ **点消息任意处**就能收回 |
>
> 所以这不是"新的取代旧的"，是长短互补：**内容短、只是不想占地方 → `details`；
> 内容长、用户读完要随手收 → 可展开引用块。** 谁当默认，最好让用户自己挑。

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
| `map` | `location{latitude,longitude}`, `zoom` 0-24, `width`, `height`, `caption?` | 地图 | ✅ |
| `collage` | `blocks[]`（photo 块数组）, `caption?` | 多图拼贴 | ✅ |
| `slideshow` | `blocks[]`（photo 块数组）, `caption?` | 轮播 | ✅ |
| `photo` | `photo`(InputMediaPhoto) + `caption?` | 图片块（外链 / file_id / attach:// 三来源全验过） | ✅ |
| `video` `animation` `audio` `voice_note` | 对应媒体 + `caption?` | 媒体块 | 📖 |
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

三条路（blocks 路线全部实测 ✅）：

1. **外部 URL**（只支持 HTTP/HTTPS）：photo 块的 `media` 直接填链接；
   markdown 里是 `![](https://example.com/photo.jpg)`。媒体必须**单独成块**，
   不能嵌在段落里。
2. **attach:// 上传本地文件**：multipart 里带文件（字段名自取，如 `f0`），
   photo 块的 `media` 填 `attach://f0`。这就是「一条消息拼九宫格」的做法：
   collage 的 blocks 里放 N 个 photo 块，各指一个 attach。
3. **file_id 复用**：发送成功的响应里，每张图带一组尺寸变体（各有 file_id，
   取面积最大那档）。存下来之后 `media` 直接填 file_id，**不用重新上传**。
   ⚠️ file_id 必须**整串程序化取用**——它们彼此只差尾部几个字符，看着截断的
   显示手补尾巴等于自己编标识符，命中全靠运气（真发生过，侥幸没炸）。

markdown / html 路线的媒体引用是另一套：走 `InputRichMessage.media` 数组 +
正文里 `tg://photo?id=…` / `tg://video?id=…` / `tg://audio?id=…`（📖 未实测）。

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

### 本地图九宫格（一条消息，不是九条）

```json
{"type": "collage",
 "caption": {"text": "六张拼一条", "credit": "attach:// 直传"},
 "blocks": [
   {"type": "photo", "photo": {"type": "photo", "media": "attach://f0"}},
   {"type": "photo", "photo": {"type": "photo", "media": "attach://f1"}}
 ]}
```

multipart 里带 `f0`/`f1` 两个文件。`collage` 换 `slideshow` 就是左右翻页。
配套 MCP 的话就是 `media_paths=[路径…]`，第 i 个路径＝`attach://f{i}`。

### 地图（发个坐标）

```json
{"type": "map", "location": {"latitude": 63.4044, "longitude": -19.0588},
 "zoom": 12, "width": 800, "height": 500,
 "caption": {"text": "Reynisfjara 黑沙滩", "credit": "坐标现查，别背"}}
```

`width`/`height` 总和 ≤10000、比例 ≤20；`zoom` 0-24。

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
| **转发给 agent** | ⚠️ **取决于收信侧管道**：作者当时用的那个入站插件只把纯文本层透给 agent（**本次没做转发收包实测**，别当成通用结论） |

复制塌陷的原因很确定：表格、折叠块、公式、spoiler 都是**客户端渲染**的，消息传的
是结构描述，系统剪贴板只接得住纯文本那一层；转发是把整条消息原样递过去，结构还在。

但"转发给 agent 就只剩纯文本"**不是** Bot API 的普遍事实：官方 `Message` 对象**有
`rich_message` 字段**（见[官方 Message 定义](https://core.telegram.org/bots/api#message)），
能不能读到结构取决于**收信侧的插件/管道有没有把它透出来**——不能由"某个插件丢了字段"
推出"所有 bot 都读不到结构"。作者当时用的入站插件（及其版本）没透出，仅此而已。

⇒ 三条实践规矩：

1. **要被复制走的东西别包富格式**：给用户的命令、代码、要转贴的段落，
   用普通消息或代码块更实在。
2. 想让第三方看到效果，用**转发**或截图，别让用户复制粘贴。
3. **要给 agent 传结构化数据，别赌收信插件透不透 `rich_message`**：最稳的是走
   API / 文件 / 你自己的协议；至于"转发一条富消息给 bot 能读到多少结构"，取决于
   收信侧管道，用之前先在你自己的入站栈上验一次。

这跟「渲染搬不走」（你自己的前端拿不到 Telegram 的渲染能力）是同一件事的三面：
能复用的永远是**块协议**，不是渲染结果。

## 8. 和富消息相邻、但不是同一套的东西

- **Ephemeral Messages**（10.2 起）：群里发只有指定用户和 bot 看得见的消息，
  可编辑可删，还有 `BotCommand.is_ephemeral`——命令的响应只对发起人可见。
  ⚠️ 早期（10.2）官方接收方法**不含** `sendRichMessage`；但 **10.3（2026-08-24）
  改用 `ephemeral_message_parameters` 对象，并给 `sendRichMessage` 也加了这个参数**
  ——现在"富消息"和"群内悄悄话"能是同一次调用了。**Telegram 已支持，本工具尚未
  暴露**这个参数。见[官方 10.3 更新](https://core.telegram.org/bots/api#august-24-2026)。
- **`sendDocument` / `sendPhoto`**：不包富格式的裸文件车道。上传纪律同
  attach://（定长 multipart，别流式——「文本正常、发文件必炸」多半是流式
  body 被代理掐）；和文字组合发送时分段记账、别整体重试，文字段可能已送达，
  整体重试＝用户收两遍。
- **`setMessageReaction`**：给用户的消息贴 emoji 当收讫回执（比如 👀）。
  一条实践：**回执要带信息量**——agent 空闲时几秒内就有真回复，再贴回执是
  噪音；改成**只在忙着干长活时**贴，用户一眼就知道「看见了，在干活，等会儿回」。
  emoji 必须在 TG 的反应白名单里，白名单外的会被 API 拒。
- `message_effect_id`（私聊限定的消息特效）、`protect_content`、
  `skip_entity_detection`（关掉 URL/邮箱/命令的自动识别）、
  `message_thread_id`、business connection、suggested post、付费广播参数。

---

## 9. 贴纸：agent 的情绪通道（相邻车道，不是富消息）

富消息负责把内容排得好看，贴纸负责**脸**。一个配好标签的贴纸库，等于给 agent
一套可复用的表情。这章是我们自用系统沉淀的思路与坑——实现耦在自家频道插件里
搬不动，思路全部通用。

### 身份三定律（先背这个，别的都好说）

| 字段 | 稳定性 | 用途 |
|---|---|---|
| `file_id` | **会变**（重传就换、换 bot token 全作废），且**绑定 bot** | 只用它**下载和发送** |
| `file_unique_id` | 跨 bot、跨时间恒定 | 只用它**判定身份**（不能拿来下载） |

1. **认人认 `file_unique_id`，发送用 `file_id`**，分工不可互换。拿一堆会过期的
   file_id 当身份，等于用门牌号拼身份证。
2. **贴纸和图片是两条车道**：贴纸的 file_id 只能 `sendSticker` 直投，塞进富消息
   photo 块会被拒；photo 的 file_id 也进不了 sendSticker。
3. **file_id 是「这个 bot」的资产**。多个 bot 共用一套库时，共享层放原图 +
   `file_unique_id` + 标签；每个 bot 首次用某张时从原图上传、把返回的 file_id
   缓存在自己名下（懒迁移，不用手工逐张重传）。**只有 Telegram 回 400 且文案命中
   "file_id 失效"那类原因才算 ID 失效**、才触发重传；网络错/限流/5xx 都不算——
   而且 400 是个杂物袋（`chat not found`、参数错也报 400），**光凭 400 就重传会白传
   一遍归档、还盖掉真错因**，所以要在 400 之内再按文案收窄（与 `_looks_file_id_400`
   一致）。判错了会重复上传。

### 库的最小形状

```jsonc
{"stickers": [{
  "title": "敲键盘炸毛猫",
  "desc": "猫毛全立起来趴在键盘上，一脸怒气",
  "tags": ["生气", "写代码", "炸毛"],
  "emoji": "😾",                  // 主 emoji：清单里显示的那个
  "emojis": ["💢", "💻"],         // 别名：额外入口，只加入口不改主映射
  "file": "img/007.webp",         // 原图归档（跨 bot 上传的源）
  "file_unique_id": "AgAD…"
}]}
```

**别名的负担在库上，不在 agent 脑子里**：agent 只写此刻感觉到的那个 emoji，
库里挂的别名越多越容易命中，纯收益。

### 入站：新贴纸让 agent 自己下载、自己标

1. 用户发来贴纸 → 消息里带 `file_id` / `file_unique_id`（还有贴纸作者标的
   emoji，当线索）；
2. 按 `file_unique_id` 查库：**命中** → 把标题+标签+描述直接给 agent，
   不用重新看图；**没命中** → 自动落进「待认领区」（记两个 id + emoji 线索 +
   见过几次），后台 `getFile` 把原图拉下来；
3. agent 得空时看图**起标题、写描述、配 emoji 标签**入库。归档可以自动化，
   **审美不要自动化**——标签是看着图写出来的，才配得上「情绪索引」这个用途。

两个细节：同一张反复出现就计数抬优先级（反复用＝喜欢＝早点认领）；
`.tgs` 动画贴纸是 gzip 的 Lottie 矢量，多数管线看不了画面——如实标
「动画贴纸，内容待补」，别硬编一个描述。

### 出站：emoji 是索引键，不是描述

图片的信息住在 `desc`/`tags` 里；emoji 键的美德恰恰是**粗、快、允许歧义**：

- **一个 emoji**＝那一池里随机挑，并避开上一次刚发的那张 → 同一个情绪的脸
  自动有变化。多张贴纸挂同一个 emoji 不是冲突，是特性——同一张脸连发五次
  才是真的死板。
- **多个 emoji**＝取交集、越写越窄：`😾` 是生气池里随机，`💻😾` 就只剩
  敲键盘炸毛那张，等于点名。粗细连续可调，没有「反射/精确」两套语法。
  顺序无关（内部按码点排序归一，两种写法共用同一条「上次发过哪张」的记忆）；
  ZWJ 组合 emoji **整串优先查表**，查不到再按字素簇拆，别按码位切。
- **交集为空＝想说的那张库里没有**：什么都不发，比发一张沾边的强——发错脸
  是说错话，不发是没说话。别做「退一步取泛指集」的回落：泛指会把交集刚挣来
  的精确又冲掉，还抹平了近义 emoji 之间真实的差别。
- **判断标准只有一条：交集里剩下的每一张，感觉都得对。剩几张不要紧。**
  唯一性是手段，情感命中才是目的。

### 孤儿脸：分段发送藏着的一格

句内标记把一条消息切成「文字段＋贴纸段」按顺序发，于是有一格很容易漏：标记
写在句首（`（🥺）我错了`）时贴纸段排在最前面，若老实按顺序发——贴纸成功、
正文随后失败，对方就收到**一张莫名其妙的表情，然后什么都没有**。

这比「少一张脸」严重。整套设计的地基是位置即语义：脸是贴给它前面那句话的。
缺脸只是少了语气；**孤儿脸是把语气安在了一句不存在的话上**，对方会对着一张
贴纸猜你到底想说什么。规则四条：

| 情形 | 行为 |
|---|---|
| 整条没有正文段（纯贴纸） | 照常直发——没有「所依附的那句话」，不存在孤儿 |
| 有正文，但还没有任何一条送达 | 贴纸**挂起，不发** |
| 第一条正文落地 | 立刻补发挂起的脸（位置仍在它该在的那句之后） |
| 正文中途抛错 | 整条中断，挂起的脸**永不发送**——宁可对方什么都没收到 |

反过来**不变**：贴纸自己发失败依旧不牵连正文、不自动重试（坑 17 的老纪律）。
话送到了比脸送到了要紧——两条规则一顺一逆，合起来才是完整的送达纪律。

### 反应通知：让点赞落回具体那张脸上（收信侧配方）

用户给贴纸消息点了个 ❤，收信侧通知 agent 时引用的预览往往是**裸 emoji**——
agent 只知道「有张脸被点赞了」，不知道是哪张。被点赞的明明是「敲键盘炸毛猫」
这个具体的表达，反馈却退化成了「😾 被赞」，白瞎了库里认真起的标题。

配方很薄：**发送时顺手记一张 `file_id → 标题` 的表**（本仓的库其实已经存够了
反查的料，但方向要看清：`file-ids.<bot_id>.json` 磁盘上存的是 **unique→file_id**
（`remember_file_id` 写 `cache[unique]=file_id`），所以拿 file_id 反查身份得**倒置**
这张表；再用 `library.json` 的 unique→title 第二跳回到标题）；收信侧拿到反应通知时
按 id 查表，把通知写成「❤ → 贴纸『敲键盘炸毛猫』」。

⚠️ 这层**住在收信侧**，本仓（一个发送工具）够不着——反应通知从哪个频道插件
进来，就得打在哪个插件上。我们的参考实现挂在 Claude Code 官方 Telegram
频道插件上，属于那条车道的专属改造；换别的 host/频道，思路照抄、代码另写。

### 为什么值得做：把「决定」变成「反射」

「按情绪词挑一张发」的一步到位工具我们早就有，但 agent 很少用。卡点不在麻烦，
在于它是个**决定**：话说完，还得另起一个念头「要不要配张图」，再挑词、再调
一次工具。后来改成让 agent 在正文里顺手写一个 emoji 标记、出站时剥成真贴纸，
写到哪儿贴纸就跟在哪条后面（位置即语义）——用了才知道，**发贴纸的成本低到
成为反射，脸才会真的常在**。那层出站过滤器耦死在频道插件里搬不走，但这条
结论是通用的。

（配套工具已随包发布：`tg_sticker_import` / `tg_sticker_send` 一对，加上挂在
`tg_rich_send` markdown 出口上的句内标记层——用法与开关见 README「贴纸」一节。
自用版的实测结论顺带留在这儿：工具层是地基，**标记层才是让 agent 真的常用
贴纸的那层**——调工具是个「决定」，句内标记是「反射」。）

### 多实现在跑？拿 conformance fixtures 对表

这套标记语法一旦有第二份实现（我们自己就有：本仓 Python + 一份耦在频道插件里的
TS），行为就会悄悄漂——同一条正文这边发贴纸那边不发，用户看到的是玄学。我们
真漂过三处：非贴纸条目滤不滤、变体选择符 VS15 剥不剥、标记内容长度上限（16 vs
32 码位）。解法在 `sticker-spec/`：合成库 + resolver/split golden 用例，**规格长在
用例里**，每份实现自带 runner 跑同一份 fixtures（本仓的是 `test_conformance.py`）。
两条纪律：runner 只喂完整管线、不复刻 resolver 逻辑（抄本会漂移）；已知不符用
`known` 标记做**严格 xfail**——修好后 XPASS 算失败，逼着摘牌，免死金牌不许过期。

---

## 10. 按钮问答：把「问用户」变成一次工具调用（相邻车道·实验性）

富消息负责把内容排得好看，贴纸负责脸，**按钮负责收答案**。
`tg_ask_choice` 是 inline keyboard + `getUpdates` 长轮询的同步封装：
发题 → 工具内部等点击 → 返回值就是 ta 选的那个。装法/布局规则/
专用 bot 前提/实验性声明见 README「按钮问答」一节，这里只放配方和设计结论。

### 配方：让 agent 出题收答案

```
tg_ask_choice(question="这道逻辑题选哪个？", options=["A", "B", "C", "D", "E"])
→ {"index": 2, "option": "C", "message_id": 456}
```

适合的场合：对错当场判（答案在 agent 手里）、方案二选一、
「要不要继续」的确认、菜单点单。选项超过 16 字（中文计）会自动切
「正文列全文+编号按钮」——那是真机实测的像素剪刀线，别跟它赌。

（同一层机制之上还设计过手机权限审批卡 `tg_ask_permission`——
`--permission-prompt-tool` 指过来，手机就是 Claude Code 的权限对话框。
参考实现进过仓又主动下架：**没实弹验证过稳定性的东西不上架**，
设计红线与复活路径见 README「还没做的」。）

### 设计结论（踩过才知道的）

- **同步比异步省三层楼**。自用版的选择题是异步双件套：发送工具落盘单据、
  回流补丁监听 callback、再经 notification 唤醒一轮——因为官方插件独占着
  getUpdates。公开版 bot 归 MCP 独用，「等答案」可以整个折进工具调用里：
  零落盘、零补丁、零唤醒车道。**架构差异来自约束差异**，不是谁写得更好。
- **选项文字绝不进 callback_data**（64 字节硬上限），只带
  `ask:<nonce>:<序号>`；nonce 认单，上一题的幽灵按钮点了只会收到「过期了」。
- **发题前先清积压**（`offset=-1` 空转一次）：上一题超时后的迟到点击，
  不许算进这一题。
- **收按钮是义务**：没人再轮询的键盘，点了永远转圈。所以默认选完就收
  （`mark_answered`），超时也收。
- **谁能点要想清楚**：私聊只认对面那个人。（这条在权限审批那边更狠：
  群聊直接拒绝发卡——「谁都能点允许」的审批不是审批。）
- **没实测过的不上架**。显示布局有真机四组对照垫底，轮询层只有单元测试——
  所以选择题标实验性上架收反馈，权限卡整个待在 TODO 里等实弹。
  两者的差别在代价：选择题坏了是重问一次，权限卡坏了是放行了不该放的调用。

## 11. 官方资料

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
