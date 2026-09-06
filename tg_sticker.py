#!/usr/bin/env python3
"""tg_sticker —— 贴纸车道：库、认领、挑选、懒迁移、句内标记。

设计要点（背景与判断标准见 COOKBOOK「贴纸」一章）：

- **认人认 `file_unique_id`（跨 bot 恒定），发送用 `file_id`（绑 bot、会变）**，
  分工不可互换。
- **file_id 是「这个 bot」的资产**：按 bot 分开缓存（file-ids.<bot_id>.json）；
  只有 Telegram 明确回 400 才判 ID 失效、才从归档原图重传。
  网络错/限流/5xx 都不算——判错了会重复上传。
- **emoji 是索引键不是描述**：一个 emoji＝池里随机（避开上次刚发的那张）；
  多个 emoji＝交集收窄；交集为空＝不发（发错脸是说错话，不发是没说话）。
- 本模块**不碰网络**：Bot API 调用（api / download）由 server 注入，
  测试直接换成假的。

⚠️ 这条管道属于 MCP server，**绝不能往 stdout 打字**。调试写 stderr。
"""

from __future__ import annotations

import errno
import json
import os
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

# 跨进程锁的平台原语：Unix 用 fcntl.flock、Windows 用 msvcrt.locking。
# 两个各平台只有一个能 import 成功，缺一不报错——由 `_CrossProcessLock` 按可用性分派。
try:                       # Unix / macOS / WSL
    import fcntl as _fcntl
except ImportError:        # 原生 Windows
    _fcntl = None          # type: ignore[assignment]
try:                       # 原生 Windows
    import msvcrt as _msvcrt
except ImportError:        # Unix
    _msvcrt = None         # type: ignore[assignment]

STICKER_EXTS = (".webp", ".tgs", ".webm")
STICKER_MAX_BYTES = 10 * 1024 * 1024   # 贴纸不该有 10MB，超了多半拿错了文件
DEFAULT_MARKER_MAX = 3                  # 一条消息最多剥几张，多出来的原样留在正文

_VARIATION_SELECTORS = {"\ufe0f", "\ufe0e"}   # VS16 / VS15
_ZWJ = "\u200d"   # zero-width joiner
_SKIN_LO, _SKIN_HI = "\U0001F3FB", "\U0001F3FF"
_RI_LO, _RI_HI = "\U0001F1E6", "\U0001F1FF"   # regional indicator（旗帜的两半）


class ApiRejected(RuntimeError):
    """Telegram API 明确拒收（payload.ok == false）。

    `code` 是 error_code。挑「file_id 失效」**先**认 400（网络错/限流/5xx
    一律不算），400 之内再用 `_looks_file_id_400` 收窄——因为 400 是个杂物袋，
    chat 不存在、参数错也报 400，那些跟 file_id 半点关系没有（澄 2026-09-04
    审出：generic 400 全当 ID 失效会白传一遍归档、还把真错因盖掉）。
    """

    def __init__(self, description: str, code: int = 0) -> None:
        super().__init__(description)
        self.code = code


# 400 之内点名「这真是 file_id 的事」的文案信号（全小写比对）。
# 与「别拿报错文案当接口」的老规矩不冲突——错误码仍是主闸（非 400 永不重传），
# 文案只在 400 内部做**放行名单**：认得出的才重传，认不出的原样抛出去
# （fail-loud——宁可用户看见真错误，也不做一次白费的上传去盖住它）。
# Telegram 出了新的 file_id 失效文案导致这儿漏放行时，把那句加进来即可。
_FILE_ID_400_SIGNS = (
    "file id",           # "wrong file identifier/HTTP URL specified" 也含这段
    "file_id",
    "file reference",
    "file_reference",    # FILE_REFERENCE_EXPIRED 这类下划线体
    "remote file",       # "wrong remote file identifier specified"
)


def _looks_file_id_400(exc: ApiRejected) -> bool:
    if exc.code != 400:
        return False
    text = str(exc).lower()
    return any(sign in text for sign in _FILE_ID_400_SIGNS)


# ---------- 跨进程互斥（贴纸库 / 缓存 / 避重状态的读—改—写）----------
# 库目录默认是**共享的**：多个会话、多只 bot 的 MCP 进程会同时读—改—写同一份
# library.json / file-ids.*.json / state.json。原子替换只挡「半截 JSON」，挡不住
# 「读—改—写丢失更新」——A、B 先后读到同一份旧库，各自 append 一条再写回，
# 后写的把先写的整条覆盖（B1）。这里加一把**跨进程**锁把整段事务罩住。
#
# 用**系统锁**（Unix flock / Windows msvcrt.locking），不用「O_EXCL 建锁文件 +
# mtime 超龄夺回」的租约方案。租约方案的病灶（R3）：mtime 老了不证明持有者死了
# ——活进程被暂停/慢盘/系统挂起超过阈值后照样醒来继续写，于是两个持有者同时进
# 临界区；而且旧持有者退出时无条件 unlink 会把接替者刚建的锁一并删掉。系统锁
# 由内核随**打开的句柄**记账：句柄关闭（进程正常退出、崩溃、被 kill）内核自动
# 释放，无需靠时间猜死活，也就不存在「夺错锁」。
#
# 锁文件是**固定文件**：`<path>.lock`，O_CREAT|O_RDWR 打开，整个生命周期
# **既不删除也不替换**（业务 JSON 仍走原子替换，与锁文件是两回事）。取消了所有
# mtime 过期回收逻辑。
#
# 🔴 迁移注意：升级时**旧版写入进程必须全部退出**，新旧两套锁协议不能混跑——
# 旧版把常驻的锁文件当「超龄死锁」unlink 掉，新版正持着它，互斥就破了；换个锁
# 文件名也没用（旧版仍会去建/删它认识的那个名字，两拨人各锁各的照样并发写）。
#
# ⚠️ Linux flock 的句柄若被 fork 出的子进程继承，父进程即使死了、只要子进程还
# 攥着那个继承来的句柄，锁就仍被持有。故**持锁期间不要创建会继承该句柄的子进程**
# （本模块持锁段是纯本地文件读写，不 fork）。
_LOCK_SPIN_SECONDS = 0.02      # 抢不到时每次自旋歇多久
_LOCK_WAIT_SECONDS = 10.0      # 抢锁总超时——真抢不到就抛，别无限期挂住调用方


class _CrossProcessLock:
    """给某个状态文件配一把固定 `<path>.lock` 的系统互斥锁（上下文管理器）。

    不同文件用不同锁；嵌套只在「库锁内再拿缓存锁」这一种固定顺序发生，
    无环故不死锁。抢锁失败会抛 TimeoutError——调用方（import 提交等）据此
    把这次操作当失败报出去，好过静默丢数据。

    两端统一「非阻塞尝试 + 有界等待」：Unix `flock(LOCK_EX|LOCK_NB)`、Windows
    `msvcrt.locking(LK_NBLCK)`，用 `time.monotonic()` 管截止时间（不用 Windows
    的 `LK_LOCK`——那个自带每秒重试最多十次，不受我们的超时管辖）。只有明确的
    锁竞争错误才继续等，其它错误直接上抛；抢不到锁绝不继续写。
    """

    def __init__(self, target: Path) -> None:
        self.lock_path = target.with_name(target.name + ".lock")
        self._fd: int | None = None

    @staticmethod
    def _try_lock(fd: int) -> bool:
        """对已打开的句柄做一次**非阻塞**独占加锁。

        拿到 ⇒ True；被别人占（明确的锁竞争 errno）⇒ False；其它 OSError 上抛
        （抢不到锁不许当没事继续）。
        """
        if _fcntl is not None:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    return False
                raise
        if _msvcrt is not None:
            # 固定锁第 0 个字节：加/解锁前都 seek 到偏移 0、长度固定 1，不依赖当前
            # 文件指针。空文件允许锁 EOF 之外区域，不必预写占位字节。
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                # 竞争：LK_NBLCK 拿不到立刻抛，errno 视运行库为 EDEADLOCK/EACCES/EAGAIN。
                if exc.errno in (errno.EDEADLOCK, errno.EACCES, errno.EAGAIN):
                    return False
                raise
        # 两个平台原语都没有（不该发生在支持的平台）：退化为无跨进程互斥。
        return True

    def __enter__(self) -> "_CrossProcessLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            # 每次尝试都用**独立打开**的句柄；只有成功加锁后才留住它，否则立刻关闭，
            # 绝不泄漏（正常退出、加锁竞争、异常上抛三条路都覆盖）。
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                got = self._try_lock(fd)
            except BaseException:
                os.close(fd)
                raise
            if got:
                self._fd = fd
                return self
            os.close(fd)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"抢贴纸状态锁超时（{self.lock_path.name}）——"
                    "有别的进程长时间持锁，这次没入库/没写，稍后重试"
                )
            time.sleep(_LOCK_SPIN_SECONDS)

    def __exit__(self, *exc: Any) -> bool:
        fd = self._fd
        self._fd = None
        if fd is None:
            return False       # enter 超时/异常时没有句柄可清，安全空转
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)       # 固定锁文件从不 unlink——见上方类前注释的迁移注意
        return False


# ---------- emoji 处理 ----------
def _strip_vs(text: str) -> str:
    return "".join(ch for ch in text if ch not in _VARIATION_SELECTORS)


def split_clusters(text: str) -> list[str]:
    """把一串 emoji 按「字素簇」切开（简化版，够 emoji 用）。

    规则：ZWJ 连接的整串不拆（🧑‍💻 是一个）；肤色修饰跟着基字；
    两个 regional indicator 是一面旗。⚠️ 调用方应**先整串查表**，
    查不到才来这儿拆——这是 ZWJ 组合字不散架的第一道保险。
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        j = i + 1
        # 旗帜：两个 regional indicator 配一对
        if _RI_LO <= text[i] <= _RI_HI and j < len(text) and _RI_LO <= text[j] <= _RI_HI:
            j += 1
        while j < len(text):
            ch = text[j]
            if ch in _VARIATION_SELECTORS or _SKIN_LO <= ch <= _SKIN_HI:
                j += 1
                continue
            if ch == _ZWJ and j + 1 < len(text):
                j += 2   # ZWJ 连同它接的下一个基字
                continue
            break
        out.append(text[i:j])
        i = j
    return out


def _looks_textual(content: str) -> bool:
    """括号里出现字母/数字/汉字/空白 ⇒ 是普通括号话，不是贴纸标记。"""
    for ch in content:
        if ch.isspace():
            return True
        cat = unicodedata.category(ch)
        if cat.startswith("L") or cat.startswith("N"):
            return True
    return False


# ---------- 库 ----------
def sticker_dir() -> Path:
    """库目录：TG_STICKER_DIR > 配置文件 sticker_dir > ~/.tg-rich-mcp-stickers。"""
    env = (os.environ.get("TG_STICKER_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from tg_rich_mcp import _file_config
        conf = str(_file_config().get("sticker_dir") or "").strip()
        if conf:
            return Path(conf).expanduser()
    except Exception:
        pass
    return Path.home() / ".tg-rich-mcp-stickers"


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(fallback)) else fallback
    except Exception:
        return fallback


def _write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """原子写：写临时文件再替换，半截 JSON 永远不落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def _atomic_write_bytes(path: Path, blob: bytes, mode: int = 0o644) -> None:
    """把 blob 原子发布到 path：**每次调用自己的临时文件**（带 pid+随机后缀）写满
    再 `os.replace` 顶上去。

    两个作用（R1）：① 读侧永远看到「旧的完整文件」或「新的完整文件」，绝不会读到
    写了一半/被截断的中间态；② 临时文件名各调用互不相同，并发导入同一 unique 时
    谁也截不断谁的临时文件，清理时也只删自己那份。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{random.randrange(1 << 32):08x}.tmp")
    try:
        tmp.write_bytes(blob)
        try:
            tmp.chmod(mode)
        except OSError:
            pass
        os.replace(str(tmp), str(path))
    finally:
        try:
            tmp.unlink()      # os.replace 成功后 tmp 已不在；异常路径下清掉自己的临时文件
        except OSError:
            pass


def load_library() -> list[dict[str, Any]]:
    data = _read_json(sticker_dir() / "library.json", {})
    stickers = data.get("stickers") if isinstance(data, dict) else None
    return stickers if isinstance(stickers, list) else []


def save_library(stickers: list[dict[str, Any]]) -> None:
    _write_json(sticker_dir() / "library.json", {"stickers": stickers}, mode=0o644)


def key_index(stickers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """emoji 键（主 emoji + emojis[] 别名，剥掉变体选择符）→ 贴纸池。

    `kind` 非 sticker 的条目（photo 等）不进贴纸池——那是别的车道的东西，
    进来只会被 sendSticker 拒收（sticker-spec 漂移#1 裁决）。无 kind 视为 sticker
    （本仓 import 从不写 kind，这条防的是外部库直接投喂的场景）。
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in stickers:
        if str(entry.get("kind") or "sticker") != "sticker":
            continue
        keys = [str(entry.get("emoji") or "")] + [
            str(e) for e in entry.get("emojis") or [] if isinstance(e, str)
        ]
        for key in keys:
            key = _strip_vs(key.strip())
            if key:
                index.setdefault(key, []).append(entry)
    return index


def _bot_id(token: str) -> str:
    """token 冒号前那截是 bot 的公开数字 id，不是秘密——拿它当缓存文件名。"""
    head = token.split(":", 1)[0]
    return head if head.isdigit() else "default"


def _cache_path(token: str) -> Path:
    return sticker_dir() / f"file-ids.{_bot_id(token)}.json"


def load_cache(token: str) -> dict[str, str]:
    return _read_json(_cache_path(token), {})


def remember_file_id(token: str, unique_id: str, file_id: str) -> None:
    # 读—改—写全程持锁：多进程/多 bot 同时缓存 file_id 时，无锁的后写会
    # 覆盖先写的（丢别的 unique 的缓存条目）。锁按缓存文件本身。
    path = _cache_path(token)
    with _CrossProcessLock(path):
        cache = _read_json(path, {})
        cache[str(unique_id)] = str(file_id)
        _write_json(path, cache)


# ---------- 挑选 ----------
def resolve_emoji(content: str, index: dict[str, list[dict[str, Any]]]
                  ) -> tuple[list[dict[str, Any]], str] | None:
    """emoji 串 → (候选池, 归一化组合键)。认不出 / 交集为空 ⇒ None。

    整串优先查表（ZWJ 组合字不散架），查不到才按字素簇拆、逐个查、取交集。
    组合键按码点排序归一：（📖😭）和（😭📖）共用同一条「上次发过哪张」的记忆。
    """
    norm = _strip_vs(content.strip())
    if not norm or _looks_textual(norm):
        return None
    if norm in index:
        return list(index[norm]), norm
    parts = split_clusters(norm)
    if len(parts) < 2:
        return None
    pools: list[list[dict[str, Any]]] = []
    for part in parts:
        pool = index.get(part)
        if not pool:
            return None   # 有一个不在库里 ⇒ 整个标记不认
        pools.append(pool)
    ids = {str(e.get("id")) for e in pools[0]}
    for pool in pools[1:]:
        ids &= {str(e.get("id")) for e in pool}
    if not ids:
        return None       # 交集为空 ⇒ 想说的那张库里没有，不硬找
    combo = "".join(sorted(set(parts)))
    return [e for e in pools[0] if str(e.get("id")) in ids], combo


def pick(pool: list[dict[str, Any]], combo_key: str) -> dict[str, Any]:
    """池里随机挑一张，避开这个组合键上次刚发的那张（脸自动有变化）。"""
    state_path = sticker_dir() / "state.json"
    # 读—改—写持锁：并发发送同一组合键时，无锁的后写会覆盖别的键的避重记忆。
    with _CrossProcessLock(state_path):
        state = _read_json(state_path, {})
        last = state.get(combo_key)
        candidates = [e for e in pool if str(e.get("file_unique_id")) != str(last)]
        entry = random.choice(candidates or pool)
        state[combo_key] = str(entry.get("file_unique_id"))
        _write_json(state_path, state)
    return entry


# ---------- 发送（懒迁移） ----------
def _archive_file(entry: dict[str, Any]) -> Path:
    """归档原图必须真实住在库目录树内（符号链接按真实目标查）、扩展名合法。"""
    base = sticker_dir().resolve()
    real = (sticker_dir() / str(entry.get("file") or "")).resolve()
    if not str(real).startswith(str(base) + os.sep):
        raise RuntimeError(f"贴纸 {entry.get('id')} 的归档原图不在库目录内，拒发")
    if real.suffix.lower() not in STICKER_EXTS:
        raise RuntimeError(f"贴纸 {entry.get('id')} 的归档不是贴纸格式：{real.suffix}")
    if not real.is_file():
        raise RuntimeError(
            f"贴纸 {entry.get('id')}「{entry.get('title')}」的归档原图不见了：{real.name}"
        )
    return real


class StickerReceipt:
    """一次贴纸发送的**结构化回执**（R2）。

    `status` 三态，与文字段一套口径：
      - `"delivered"`：服务器已确认收下（带 `message_id`）。**发送后的缓存维护
        失败不推翻它**——记 delivered，另挂 `cache_warning`（下次可能再上传一遍，
        但这条已经送到了，绝不能误报成没发）。
      - `"failed"`：服务器**明确拒收**（ApiRejected/ok:false），或发送前的本地问题
        （归档不在库/格式不对/原图丢失）——肯定没发出去，可安全补这一段。
      - `"unknown"`：**网络错/超时**（请求可能已到 Telegram），送达状态未知，
        禁止当没发出去自动补发。

    `exc` 留着原始异常，好让向后兼容的 `send_entry()` 包装层把**原类型**照原样抛出去。
    """

    def __init__(self, status: str, note: str, *, message_id: int | None = None,
                 cache_warning: str | None = None, error: str | None = None,
                 exc: BaseException | None = None) -> None:
        self.status = status
        self.note = note
        self.message_id = message_id
        self.cache_warning = cache_warning
        self.error = error
        self.exc = exc


def _sticker_mid(payload: dict[str, Any]) -> int | None:
    result = payload.get("result") if isinstance(payload, dict) else None
    mid = result.get("message_id") if isinstance(result, dict) else None
    return mid if isinstance(mid, int) else None


def send_entry_receipt(entry: dict[str, Any], chat_id: str, token: str,
                       api: Callable[..., dict[str, Any]],
                       silent: bool = False) -> StickerReceipt:
    """v3 懒迁移，返回**结构化回执**（不为发送结果抛异常，全部落进 status）。

    区分四个阶段：本地预检（failed）、服务器拒收（failed）、网络未知（unknown）、
    发送成功后的缓存维护（delivered，缓存失败只挂告警）。`silent` 与分段正文共享
    同一发送选项，别让"命中贴纸分支"把 disable_notification 丢掉（B7）。
    """
    title, emj = entry.get("title"), entry.get("emoji")
    unique = str(entry.get("file_unique_id") or "")
    quiet = {"disable_notification": "true"} if silent else {}
    cached = load_cache(token).get(unique) if unique else None
    if cached:
        try:
            payload = api("sendSticker", {"chat_id": chat_id, "sticker": cached, **quiet})
        except ApiRejected as exc:
            if not _looks_file_id_400(exc):
                # 非 400，或 400 但不关 file_id 的事（chat 错/参数错）＝服务器拒收
                return StickerReceipt("failed", f"贴纸未送达（服务器拒收）：{title}（{emj}）",
                                      error=str(exc), exc=exc)
            # 文案点名 file_id 失效（换过 token 等），才走归档重传（往下走）
        except (RuntimeError, OSError) as exc:
            return StickerReceipt("unknown", f"贴纸送达状态未知（网络错）：{title}（{emj}）",
                                  error=str(exc), exc=exc)
        else:
            return StickerReceipt("delivered", f"贴纸已发：{title}（{emj}）",
                                  message_id=_sticker_mid(payload))
    # ---- 归档重传：本地预检 → 上传 → 缓存新 file_id ----
    try:
        real = _archive_file(entry)
        blob = real.read_bytes()
    except (RuntimeError, OSError) as exc:
        # 发送前的本地问题（归档不在库/格式不对/原图丢失）＝一个字节都没出门 → failed
        return StickerReceipt("failed", f"贴纸未送达（归档不可用）：{title}（{emj}）",
                              error=str(exc), exc=exc)
    try:
        payload = api("sendSticker", {"chat_id": chat_id, **quiet},
                      files={"sticker": (real.name, blob)})
    except ApiRejected as exc:
        return StickerReceipt("failed", f"贴纸未送达（服务器拒收）：{title}（{emj}）",
                              error=str(exc), exc=exc)
    except (RuntimeError, OSError) as exc:
        return StickerReceipt("unknown", f"贴纸送达状态未知（网络错）：{title}（{emj}）",
                              error=str(exc), exc=exc)
    # 到这儿服务器已收下——从此**不许**再把它降级成失败/未知。
    result = payload.get("result") or {}
    fresh = ((result.get("sticker") or {}).get("file_id")
             if isinstance(result, dict) else None)
    cache_warning = None
    if unique and fresh:
        try:
            remember_file_id(token, unique, str(fresh))
        except OSError as exc:
            # 缓存写盘失败（磁盘满等）：已送达不受影响，只是下次可能再上传一遍。
            cache_warning = f"file_id 缓存写入失败（{exc}）——本次已送达，下次可能再从归档上传一遍"
    note = f"贴纸已发：{title}（{emj}）（本 bot 首次用这张，已从归档上传并缓存 file_id）"
    if cache_warning:
        note += f"\n⚠️ {cache_warning}"
    return StickerReceipt("delivered", note,
                          message_id=_sticker_mid(payload), cache_warning=cache_warning)


def send_entry(entry: dict[str, Any], chat_id: str, token: str,
               api: Callable[..., dict[str, Any]], silent: bool = False) -> str:
    """向后兼容包装：成功返回人话 note，失败照原异常类型抛出。

    独立工具 `tg_sticker_send` 仍按「成功给文案、失败抛异常」的老合同用它；
    分段路径（`_send_with_stickers`）改用 `send_entry_receipt` 拿结构化状态。
    """
    receipt = send_entry_receipt(entry, chat_id, token, api, silent=silent)
    if receipt.status == "delivered":
        return receipt.note
    if receipt.exc is not None:
        raise receipt.exc
    raise RuntimeError(receipt.error or receipt.note)


# ---------- 工具：tg_sticker_send ----------
def _listing(stickers: list[dict[str, Any]]) -> str:
    if not stickers:
        return ("贴纸库还是空的。收到贴纸时用 tg_sticker_import 入库"
                "（agent 自己看图起标题、配 emoji 标签）。")
    lines = []
    for e in stickers:
        aliases = "".join(str(a) for a in e.get("emojis") or [])
        tags = "/".join(str(t) for t in e.get("tags") or [])
        lines.append(f"  {e.get('id')}. {e.get('emoji')}{aliases} {e.get('title')}"
                     + (f"（{tags}）" if tags else ""))
    return (f"馆藏 {len(stickers)} 张：\n" + "\n".join(lines)
            + "\n发送：emoji 挑张（多个 emoji＝交集收窄）／ id 直取 ／ query 按词搜。")


def tool_sticker_send(args: dict[str, Any], chat_id: str, token: str,
                      api: Callable[..., dict[str, Any]]) -> str:
    stickers = load_library()
    emoji = str(args.get("emoji") or "").strip()
    query = str(args.get("query") or "").strip()
    raw_id = str(args.get("id") if args.get("id") is not None else "").strip()

    if not emoji and not query and not raw_id:
        return _listing(stickers)
    if not stickers:
        return _listing(stickers)

    if raw_id:
        if not raw_id.isdigit():
            raise ValueError("id 必须是馆藏编号（先不带参数调一次看清单）")
        matches = [e for e in stickers if str(e.get("id")) == raw_id]
        if not matches:
            raise ValueError(f"馆藏里没有 {raw_id} 号（先不带参数调一次看清单）")
        if str(matches[0].get("kind") or "sticker") != "sticker":
            raise ValueError(
                f"馆藏 {raw_id} 号是 {matches[0].get('kind')} 不是贴纸——"
                "这条车道只发贴纸，sendSticker 会拒收它")
        return send_entry(matches[0], chat_id, token, api)

    if emoji:
        resolved = resolve_emoji(emoji, key_index(stickers))
        if resolved is None:
            raise ValueError(
                f"emoji「{emoji}」在库里没命中（不在库/交集为空）。"
                "不硬找——发错脸比不发更糟。先不带参数调一次看馆藏。"
            )
        pool, combo = resolved
        return send_entry(pick(pool, combo), chat_id, token, api)

    needle = query.lower()
    matches = [e for e in stickers if needle in " ".join(
        [str(e.get("title") or ""), str(e.get("desc") or "")]
        + [str(t) for t in e.get("tags") or []]).lower()]
    if not matches:
        raise ValueError(f"query「{query}」没搜到（搜的是标题/描述/标签）。"
                         "先不带参数调一次看馆藏。")
    return send_entry(pick(matches, f"q:{needle}"), chat_id, token, api)


# ---------- 工具：tg_sticker_import ----------
def _pending_dir() -> Path:
    return sticker_dir() / "pending"


def _next_id(stickers: list[dict[str, Any]]) -> int:
    return max((int(e.get("id", -1)) for e in stickers), default=-1) + 1


def _find_by_unique(stickers: list[dict[str, Any]], unique: str) -> dict[str, Any] | None:
    for e in stickers:
        if str(e.get("file_unique_id")) == unique:
            return e
    return None


def _download_original(file_id: str, api: Callable[..., dict[str, Any]],
                       download: Callable[[str], bytes]) -> tuple[str, str, bytes]:
    """getFile → 下载原图。返回 (file_unique_id, 扩展名, 字节)。"""
    payload = api("getFile", {"file_id": file_id})
    info = payload.get("result") or {}
    unique = str(info.get("file_unique_id") or "")
    remote = str(info.get("file_path") or "")
    size = int(info.get("file_size") or 0)
    if not unique or not remote:
        raise RuntimeError("getFile 的返回缺 file_unique_id/file_path，没法归档")
    ext = Path(remote).suffix.lower() or ".webp"
    if ext not in STICKER_EXTS:
        raise ValueError(f"这不像贴纸（{ext}）——本工具只收 {'/'.join(STICKER_EXTS)}")
    if size > STICKER_MAX_BYTES:
        raise ValueError(f"文件 {size / 1024 / 1024:.1f}MB，超过贴纸车道的 10MB 上限")
    return unique, ext, download(remote)


def tool_sticker_import(args: dict[str, Any], token: str,
                        api: Callable[..., dict[str, Any]],
                        download: Callable[[str], bytes]) -> str:
    file_id = str(args.get("file_id") or "").strip()
    unique_arg = str(args.get("file_unique_id") or "").strip()
    title = str(args.get("title") or "").strip()
    emoji = _strip_vs(str(args.get("emoji") or "").strip())
    stickers = load_library()

    # ---- 认领待认领区（不用重新下载） ----
    pending_record: dict[str, Any] | None = None
    if unique_arg and not file_id:
        pending_path = _pending_dir() / f"{unique_arg}.json"
        pending_record = _read_json(pending_path, {}) or None
        if not pending_record:
            existing = _find_by_unique(stickers, unique_arg)
            if existing:
                return (f"这张早就是馆藏 {existing.get('id')} 号"
                        f"「{existing.get('title')}」了，不用重复入库。")
            raise ValueError(
                f"待认领区里没有 {unique_arg}。新贴纸请带 file_id 来（我去下载归档）。")

    # blob＝本次导入**自己持有的**原图字节（内存里的）。提交归档时从它写，绝不在
    # 锁内回头去读某个共享 pending 文件——那个文件另一个并发导入随时能截断（R1）。
    if pending_record:
        unique = unique_arg
        archive = Path(str(pending_record.get("file") or ""))
        file_id = str(pending_record.get("file_id") or "")
        if not archive.is_file():
            raise RuntimeError("待认领记录在、原图不在了——带 file_id 重新导一次")
        ext = archive.suffix.lower()
        blob = archive.read_bytes()   # 认领路径：把原图一次性读进内存，之后只认 blob
    else:
        if not file_id:
            raise ValueError("要么给新贴纸的 file_id，要么给待认领区里的 file_unique_id")
        unique, ext, blob = _download_original(file_id, api, download)
        existing = _find_by_unique(stickers, unique)
        if existing:
            remember_file_id(token, unique, file_id)
            return (f"认识：馆藏 {existing.get('id')} 号「{existing.get('title')}」"
                    f"{existing.get('emoji')}（file_id 已更新进本 bot 缓存）。")
        # 下载落地一份 pending 面包屑（崩在提交前也不丢下载/可被后续认领）；
        # 但它**不**再作为归档的来源，归档只认上面的内存 blob。
        _pending_dir().mkdir(parents=True, exist_ok=True)
        archive = _pending_dir() / f"{unique}{ext}"
        archive.write_bytes(blob)

    # ---- 元数据不全 ⇒ 落待认领区，等 agent 看图再来 ----
    if not (title and emoji):
        record = _read_json(_pending_dir() / f"{unique}.json", {})
        record.update({
            "file_id": file_id,
            "file_unique_id": unique,
            "file": str(archive),
            "emoji_hint": str(args.get("emoji_hint") or record.get("emoji_hint") or ""),
            "seen": int(record.get("seen") or 0) + 1,
        })
        _write_json(_pending_dir() / f"{unique}.json", record)
        return (
            f"已落待认领区（第 {record['seen']} 次见到它）。原图在：\n  {archive}\n"
            "下一步：用 Read 看这张图，然后再调一次 tg_sticker_import 带上\n"
            f"  file_unique_id=\"{unique}\" + title（起个名）+ emoji（主情绪）\n"
            "  可选 emojis（别名入口，越多越容易命中）/ tags / desc。\n"
            "归档自动化了，审美别自动化——标签要看着图写。"
        )

    # ---- 认领入库（跨进程事务）----
    # 编号分配 + 唯一身份去重 + 馆藏写回必须在一把锁里一次做完：否则并发的两个
    # 进程各拿旧快照算出同一个 id、写同一个原图、后写覆盖先写（B1）。下载已在锁外
    # 完成；这里只做提交，且**锁内重读**库、不信锁外那份可能已过期的 stickers。
    entry: dict[str, Any] = {}
    new_id = -1
    with _CrossProcessLock(sticker_dir() / "library.json"):
        stickers = load_library()
        existing = _find_by_unique(stickers, unique)
        if existing is None:
            new_id = _next_id(stickers)
            img_dir = sticker_dir() / "img"
            img_dir.mkdir(parents=True, exist_ok=True)
            # 原图按 file_unique_id（跨 bot 恒定的稳定身份）命名，不按会撞车的顺序号：
            # 万一锁失灵，稳定名也让两张不同的贴纸落到不同文件、不互相覆盖。
            # 从内存 blob 原子发布（R1）：不 `archive.read_bytes()`——共享 pending 文件
            # 此刻可能正被另一个并发导入 `open('wb')` 截断成零字节，读到就归档空文件。
            final = img_dir / f"{unique}{ext}"
            _atomic_write_bytes(final, blob)
            entry = {
                "id": new_id,
                "title": title,
                "desc": str(args.get("desc") or ""),
                "tags": [str(t) for t in args.get("tags") or [] if str(t).strip()],
                "emoji": emoji,
                "emojis": [_strip_vs(str(e)) for e in args.get("emojis") or [] if str(e).strip()],
                "file": str(final.relative_to(sticker_dir())),
                "file_unique_id": unique,
            }
            stickers.append(entry)
            save_library(stickers)

    # 锁外收尾：缓存 file_id（缓存有自己的锁，别嵌在库锁里）、清待认领区
    if file_id:
        remember_file_id(token, unique, file_id)
    pending_json = _pending_dir() / f"{unique}.json"
    for leftover in (pending_json, *(p for p in [archive] if p.parent == _pending_dir())):
        try:
            leftover.unlink()
        except OSError:
            pass
    if existing is not None:
        # 并发下另一个进程已把它入库——认它、不重复建号（原图也已由那次归档）
        return (f"这张已经是馆藏 {existing.get('id')} 号「{existing.get('title')}」了"
                f"（并发入库已合并，file_id 也更新进本 bot 缓存）。")
    aliases = "".join(entry["emojis"])
    return (f"入库：馆藏 {new_id} 号「{title}」{emoji}{aliases}，原图归档 {entry['file']}。"
            f"现在（{emoji}）这类标记和 tg_sticker_send 都能命中它了。")


# ---------- 句内标记（渲染器模式的第二层） ----------
_MARKER_RE = re.compile(r"[（(]([^（）()\n]{1,32})[）)]")
# 代码遮罩按 **Markdown 边界**，不是"单反引号/三反引号"的简易表达式（B8）：
# ① 行首围栏（``` 或 ~~~，3+ 个）——闭合到等长同类围栏，未闭合则吃到文末；
# ② 行内反引号跨度——N 个开、**等长的 N 个**闭（`` ``（😺）`` `` 里两个反引号是一对，
#    中间的表情要遮住，旧正则把边界两个反引号各当成一段空代码、漏了中间）。
# 讨论这套语法本身时（把标记写在代码里）就不会当场喷贴纸。
_FENCE_RE = re.compile(
    r"(?m)^[ \t]{0,3}(?P<f>`{3,}|~{3,})[^\n]*(?:\n[\s\S]*?(?:\n[ \t]{0,3}(?P=f)[ \t]*$|\Z)|\Z)"
    r"|(?<!`)(?P<b>`+)(?!`)[\s\S]*?(?<!`)(?P=b)(?!`)"
)


def marker_max() -> int:
    raw = (os.environ.get("TG_STICKER_MAX") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_MARKER_MAX
    except ValueError:
        return DEFAULT_MARKER_MAX


def markers_enabled() -> bool:
    return (os.environ.get("TG_STICKER_MARKERS") or "1").strip() != "0"


def split_message(text: str) -> list[tuple[str, Any]]:
    """把正文按贴纸标记切开：[("text", 段), ("sticker", (池, 组合键, 原文)), …]。

    位置即语义：标记写在哪儿，贴纸就跟在哪条后面。
    认不出的标记原样留在正文（坏掉的时候最好看：一对普通括号，不穿帮）。
    反引号里的不算数——讨论这套语法本身时不会当场喷贴纸。
    """
    if not markers_enabled():
        return [("text", text)]
    stickers = load_library()
    if not stickers:
        return [("text", text)]
    index = key_index(stickers)

    masked = set()
    for m in _FENCE_RE.finditer(text):
        masked.update(range(m.start(), m.end()))

    parts: list[tuple[str, Any]] = []
    cursor = 0
    used = 0
    limit = marker_max()
    for m in _MARKER_RE.finditer(text):
        if used >= limit:
            break
        if m.start() in masked:
            continue
        resolved = resolve_emoji(m.group(1), index)
        if resolved is None:
            continue
        pool, combo = resolved
        chunk = text[cursor:m.start()].strip()
        if chunk:
            parts.append(("text", chunk))
        parts.append(("sticker", (pool, combo, m.group(0))))
        cursor = m.end()
        used += 1
    tail = text[cursor:].strip()
    if tail:
        parts.append(("text", tail))
    return parts or [("text", "")]
