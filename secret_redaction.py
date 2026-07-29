#!/usr/bin/env python3
"""密钥形态的单一真源。

这个文件存在的原因很具体：同一条 Telegram token 正则本来抄在两个文件里，
修的时候只改了一处，另一处就那么敞着——**漂移不是假设，是已经发生过的事**。

⚠️ 这些是**安全启发式**，不是 token 合法性验证器。Telegram 只给出示例形态、
要求你像密码一样保护它，并没有承诺一个永久不变的严格长度规范。
所以取舍是明确的：**宁可多拦，不要少拦**——摘要里少一句话没人受伤，
漏一个 token 就是别人的账号。
"""
from __future__ import annotations

import re

# 不能用 `\b` 开头：真实的泄漏长这样
#     https://api.telegram.org/bot123456789:AAH…/sendMessage
# "bot" 的 `t` 和后面的数字**都是单词字符**，两者之间没有词边界，
# 于是 `\b\d{6,}` 让最典型的那种泄漏原样漏了过去（异常消息里带整条 URL）。
#
# `(?<!\d)` 是**负向后顾**（negative lookbehind）：只要求紧邻的前一个字符不是数字。
# 这样 bot123… / token=123… / 裸的 123… 全都认得，又不会从一长串数字中间切一刀。
TELEGRAM_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")


def redact_telegram_tokens(text: str) -> str:
    """把文本里任何 Telegram bot token 形态换成 `<token>`。

    注意它认的是**形态**，所以能抹掉调用方误塞进来的、**别人的** token——
    那种精确替换（`text.replace(自家token, …)`）根本够不着。
    两层都要有：精确替换最准、形态闸兜陌生的。
    """
    return TELEGRAM_BOT_TOKEN_RE.sub("<token>", text)
