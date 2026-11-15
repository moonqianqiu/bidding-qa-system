"""工具枚举参数容错归一（单入口，docs/02 D2/D3）。

LLM 拼枚举的常见病态——截断（supplier_top）、大小写漂移、中文别名
（供应商TopN）——在工具入口处尽力自纠，坏参数不该吃掉一轮往返
（2026-11-15 空回事故加固：探针实测 3/3 轮拼错 query_type）。
与 filters 的 FILTER_ALIASES 中文别名同一设计哲学。

分辨率梯度：① strip 后精确命中 → ② 大小写不敏感命中 → ③ 唯一前缀命中
（多个候选 = 歧义，不猜）→ ④ 别名表（键大小写不敏感）→ 全部落空才抛
ValueError（文案带合法枚举清单，模型下一轮可自纠）。
"""

from __future__ import annotations


def normalize_choice(raw: str, choices: tuple[str, ...],
                     aliases: dict[str, str] | None = None,
                     *, param: str = "query_type") -> str:
    v = str(raw).strip()
    if v in choices:
        return v
    folded = v.casefold()
    for c in choices:
        if c.casefold() == folded:
            return c
    if folded:
        hits = [c for c in choices if c.casefold().startswith(folded)]
        if len(hits) == 1:
            return hits[0]
    if aliases:
        index = {k.casefold(): val for k, val in aliases.items()}
        hit = index.get(folded)
        if hit in choices:
            return hit
    raise ValueError(f"{param} 须为 {choices}")
