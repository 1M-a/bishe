"""居民诉求进入 FDE 前的数据质量处理。

这里只做可解释、可回放的确定性处理：统一全角字符、压缩空白和重复标点、
去掉常见礼貌/填充语，并基于同一手机号做精确或高相似度去重。不会擅自改写
事实内容；清洗前后的摘要和判定写入居民单字段，便于居委复核。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timedelta

from app.models import ResidentTicket


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"([，。！？、；：,.!?;:])\1{1,}")
_FILLER_RE = re.compile(
    r"^(?:您好|你好|请问|麻烦(?:您|社区)?|想反映一下|帮忙(?:看一下|处理一下)?|"
    r"辛苦了|谢谢(?:您)?)[，。！!、\s]*"
)
_TAIL_RE = re.compile(r"(?:谢谢(?:您)?|辛苦了|麻烦了)[，。！!、\s]*$")
_NUMBER_RE = re.compile(r"\d+")


def normalize_text(text: str) -> tuple[str, list[str]]:
    """返回 (清洗后文本, 可解释的处理标签)。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    flags: list[str] = []
    compact = _SPACE_RE.sub(" ", value).strip()
    if compact != value.strip():
        flags.append("空白归一")
    compact2 = _PUNCT_RE.sub(r"\1", compact)
    if compact2 != compact:
        flags.append("重复标点压缩")
    compact = compact2
    cleaned = _FILLER_RE.sub("", compact).strip()
    cleaned = _TAIL_RE.sub("", cleaned).strip()
    if cleaned != compact:
        flags.append("礼貌填充语移除")
    return cleaned or compact, flags


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _number_tokens(text: str) -> tuple[str, ...]:
    """提取楼栋、单元、房号等数字线索，防止近似去重误合并不同地点。"""
    return tuple(_NUMBER_RE.findall(text or ""))


def find_duplicate(phone: str, normalized: str, *, now=None,
                   window_days: int = 30) -> tuple[ResidentTicket | None, str, float]:
    """按同一手机号找重复诉求，返回 (原单, 判定类型, 相似度)。

    精确哈希是确定重复；近似重复仅在长度足够且相似度 >= 0.92 时命中，
    避免把同一居民的不同事项误合并。只比较最近 30 天的已受理单。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=window_days)
    rows = (ResidentTicket.query
            .filter(ResidentTicket.phone == phone,
                    ResidentTicket.created_at >= cutoff)
            .order_by(ResidentTicket.created_at.desc())
            .limit(50).all())
    digest = content_hash(normalized)
    for row in rows:
        previous = row.normalized_text or row.raw_text or ""
        previous_hash = row.content_hash or content_hash(previous)
        if previous_hash == digest:
            return row, "exact", 1.0
    if len(normalized) < 8:
        return None, "", 0.0
    number_tokens = _number_tokens(normalized)
    for row in rows:
        previous = row.normalized_text or row.raw_text or ""
        if len(previous) < 8:
            continue
        # 数字常承载楼栋、单元、房号、日期等关键事实。数字不同宁可新建，
        # 也不让高文本相似度把两个地点或两次事件合并。
        if _number_tokens(previous) != number_tokens:
            continue
        score = _similarity(normalized, previous)
        if score >= 0.92:
            return row, "similar", round(score, 4)
    return None, "", 0.0
