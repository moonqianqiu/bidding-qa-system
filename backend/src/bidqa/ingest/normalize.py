"""数值归一唯一入口（docs/01 §4.3 / docs/02 F6）：金额、地区、日期。

硬约束：金额存元（整数）；文本金额「66万」→ 660000；取不到记 None 绝不写 0
（docs/01 §2 跨层硬约束）。归一只有一个入口——所有写入方必须走本模块。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bidqa.ingest.regions import province_of  # re-export 语义：地区归一入口

__all__ = ["normalize_amount", "normalize_region", "normalize_date", "province_of"]

_NUM = r"([0-9][0-9,，]*(?:\.[0-9]+)?)"
_AMOUNT_RE = re.compile(rf"^{_NUM}\s*(?:元|人民币|CNY)?$")
_AMOUNT_WANYI_RE = re.compile(rf"^{_NUM}\s*(万|亿)\s*(?:元|人民币|CNY)?$")


def _to_number(text: str) -> float:
    cleaned = text.replace(",", "").replace("，", "")
    return float(cleaned)


def normalize_amount(raw: object) -> int | None:
    """文本金额 → 元（int）；无法解析/为空 → None（调用方记清单，不硬造 0）。

    支持：66万 → 660000；1.2亿 → 120000000；660,000元 → 660000；" 66.5 万元 " → 665000；
    纯数字（int/float/str）→ 元。负数拒绝（CHECK 同口径）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # 布尔不当数字（docs/01 §3.7 数值收口）
        return None
    if isinstance(raw, (int, float)):
        value = int(round(float(raw)))
        return value if value >= 0 else None
    text = str(raw).strip()
    if not text or text in {"-", "—", "无", "暂无", "N/A", "/"}:
        return None
    m = _AMOUNT_WANYI_RE.match(text)
    if m:
        value = _to_number(m.group(1))
        unit = 100_000_000 if m.group(2) == "亿" else 10_000
        return int(round(value * unit))
    m = _AMOUNT_RE.match(text)
    if m:
        value = int(round(_to_number(m.group(1))))
        return value if value >= 0 else None
    return None


def normalize_region(raw: object) -> str:
    """地区归一：自由文本 → 省级行政区名（按省聚合起步，决策 #9）；无匹配=空串。"""
    if raw is None:
        return ""
    return province_of(str(raw).strip())


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d", "%Y%m%d")


def normalize_date(raw: object) -> date | None:
    """日期归一：date/datetime 直通；字符串按常见格式解析；失败 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text or text in {"-", "—", "无", "/"}:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
