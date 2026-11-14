"""省级行政区词表（共享单一来源：agent/clarify 判据与 ingest/normalize 归一都用它）。"""

from __future__ import annotations

import re

PROVINCES: tuple[str, ...] = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东",
    "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海",
    "宁夏", "新疆", "香港", "澳门", "台湾",
)

_PROVINCE_RE = re.compile("|".join(PROVINCES))


def province_of(text: str) -> str:
    """从自由文本中提取省级行政区名；无匹配返回空串（取不到不硬造）。"""
    m = _PROVINCE_RE.search(text or "")
    return m.group(0) if m else ""
