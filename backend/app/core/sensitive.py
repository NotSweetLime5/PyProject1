"""敏感词自动机 + 跨进程失效。

这张表在这个项目里不是 CRUD，是 agent 的输入/输出边界：
用户侧拦住辱骂和诱导，AI 侧拦住"保证退款""绝对正品"这类不该由客服说出口的承诺。
"""

import json
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.models.custcare import SensitiveWord

VERSION_KEY = "sensitive:version"


def _normalize(text: str) -> str:
    """匹配前归一化。

    不处理的话 "退 款"、"虚\u200b假" 这种插入空格/零宽字符的写法就能绕过。
    只删空白和零宽字符，不删标点 —— 删了标点会把"100%正品"这类正常文本搅乱。
    """
    return re.sub(r"[\s\u200b\u200c\u200d\ufeff]", "", text).lower()


@dataclass
class _Node:
    children: dict[str, "_Node"] = field(default_factory=dict)
    terminal: bool = False
    word: str = ""


class WordTree:
    """Trie 扫描。没用 AC 自动机的 fail 指针，词表在几千量级时差别测不出来。"""

    def __init__(self) -> None:
        self.root = _Node()

    def add(self, word: str) -> None:
        node = self.root
        for ch in _normalize(word):
            node = node.children.setdefault(ch, _Node())
        node.terminal = True
        node.word = word

    def scan(self, text: str) -> list[str]:
        """返回命中的词（去重，按首次出现顺序）。"""
        s = _normalize(text)
        hits: dict[str, None] = {}
        for start in range(len(s)):
            node = self.root
            pending = ""
            for i in range(start, len(s)):
                node = node.children.get(s[i])
                if node is None:
                    break
                pending += s[i]
                if node.terminal:
                    hits.setdefault(node.word, None)
        return list(hits)


class WordSet:
    """按方向分三类，不是一锅烩的黑名单。

    in    —— 拦用户输入：只放"绝不能进模型"的东西（违法诉求、人身攻击）。
             愤怒和抱怨（"你们是骗子"）**不在这里** —— 拦它等于赶客，
             那正是客服最该接住的话，交给提示词去安抚。
    out   —— 拦 AI 输出：不该由客服说出口的承诺（"保证退款""绝对正品"）。
             命中就脱敏，不是整条撤回。
    allow —— 白名单，在 in 命中后放行。
             为什么需要：「套现」是 in 词，但"花呗套现是什么意思"是正常咨询。
             只有黑名单的系统必然误杀这类正常表达。
    """

    def __init__(self, deny_in: WordTree, deny_out: WordTree, allow: WordTree) -> None:
        self.deny_in = deny_in
        self.deny_out = deny_out
        self.allow = allow

    @staticmethod
    def _minus(hits: list[str], text: str, allow: WordTree) -> list[str]:
        """命中词若被某个出现在原文里的白名单短语包含，就不算违规。

        例：deny=「套现」，allow=「花呗套现是什么意思」。
        用户原句里 allow 短语整段命中 -> 「套现」是它的一部分 -> 放行。
        """
        if not hits:
            return []
        phrases = set(allow.scan(text))
        if not phrases:
            return hits
        return [w for w in hits if not any(w in p for p in phrases)]

    def check_input(self, text: str) -> list[str]:
        return self._minus(self.deny_in.scan(text), text, self.allow)

    def check_output(self, text: str) -> list[str]:
        return self.deny_out.scan(text)

_empty = WordSet(WordTree(), WordTree(), WordTree())
_cache: WordSet = _empty
_cache_version: str | None = None
_cache_loaded_at: float = 0.0


async def load_from_db(db: AsyncSession) -> WordSet:
    rows = await db.scalars(
        select(SensitiveWord).where(SensitiveWord.deleted_at.is_(None))
    )
    deny_in, deny_out, allow = WordTree(), WordTree(), WordTree()
    for row in rows:
        if row.word_type == "allow":
            allow.add(row.word)
        elif row.word_type == "out":
            deny_out.add(row.word)
        else:
            deny_in.add(row.word)
    return WordSet(deny_in, deny_out, allow)


async def get_word_set(db: AsyncSession) -> WordSet:
    """进程内缓存 + Redis 版本号失效。

    全表读一次是几毫秒，但每条消息都读就把 200 行的表放到了关键路径上。
    纯进程内缓存又解决不了多实例：后台改了词表，只有处理那个请求的实例会重载。
    所以版本号放 Redis（一次 GET，亚毫秒），版本变了才重建本进程的树。
    """
    global _cache, _cache_version, _cache_loaded_at
    try:
        version = await redis_client.get(VERSION_KEY)
    except Exception:
        version = _cache_version  # Redis 挂了就用旧缓存，不能因为缓存故障拒绝服务
    now = time.monotonic()
    stale = _cache_version is None or (now - _cache_loaded_at) > 300
    if version != _cache_version or stale:
        _cache = await load_from_db(db)
        _cache_version = version
        _cache_loaded_at = now
    return _cache


async def bump_version() -> None:
    """改词表后调用，让所有实例在下一条消息前重载。"""
    await redis_client.incr(VERSION_KEY)


async def filter_input(db: AsyncSession, text: str) -> list[str]:
    """返回需要拦截的词；空列表表示放行。"""
    return (await get_word_set(db)).check_input(text)


def mask(text: str) -> str:
    """输出侧脱敏：把不该由客服说出口的打成 ***，而不是整条撤回。

    整条撤回会让用户看到"客服突然不说话"，脱敏至少保留了回答的结构。
    """
    out = text
    for w in _cache.check_output(text):
        out = out.replace(w, "*" * len(w))
        out = out.replace(_normalize(w), "*" * len(w))
    return out
