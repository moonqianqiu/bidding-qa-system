"""B7 响应组装（docs/01 §3.4 done 字段 + §3.7 图表规格）。

来源跨轮去重合并、图表内容寻址 id、分阶段耗时（主路径与降级路径段名两套不合并）、
收尾事件。纯函数，SSE 层与评测内部层共用同一出口（口径一致）。
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

SCHEMA_V = 1


def dedup_sources(sources: list[dict]) -> list[dict]:
    """按 id 跨轮去重，保留首现顺序；返回深拷贝（缓存返回深拷贝硬约束）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for source in sources:
        key = str(source.get("id") or json.dumps(source, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(deepcopy(source))
    return out


def chart_identity(chart: dict) -> str:
    """图表内容寻址 id（docs/01 §3.7）：规范化 JSON 的 sha256 前 12 位。"""
    payload = {k: v for k, v in chart.items() if k != "id"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def sanitize_chart(chart: dict) -> dict:
    """数值三重收口（docs/01 §3.7）：小数归一/布尔不当数字/非有限数拦截；无数据不出图。"""
    items: list[dict] = []
    for item in chart.get("items") or []:
        value = item.get("value")
        if isinstance(value, bool) or value is None:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(num):
            continue
        items.append({**item, "value": round(num, 2)})
    if not items:
        return {}
    max_value = chart.get("max")
    if isinstance(max_value, bool) or not (isinstance(max_value, (int, float)) and math.isfinite(float(max_value))):
        max_value = None
    return {**chart, "items": items, "max": max_value}


def dedup_charts(charts: list[dict]) -> list[dict]:
    """内容寻址 + 去重 + 数值收口；无有效数据的图表整张不出。"""
    seen: set[str] = set()
    out: list[dict] = []
    for chart in charts:
        clean = sanitize_chart(chart)
        if not clean or not clean.get("items"):
            continue
        cid = chart_identity(clean)
        if cid in seen:
            continue
        seen.add(cid)
        out.append({**clean, "id": cid})
    return out


def build_done(result: Any, *, elapsed_ms: int, trace_id: str,
               web_sources: list[dict] | None = None,
               provider: str = "") -> dict[str, Any]:
    """done 事件字段（docs/01 §3.4）：结构化结果 + schema_v。

    澄清轮特征：三个来源字段全空 + phase_times 第二段=「澄清提问」。
    主路径 phase_times 段名：rounds_ms/generate_ms/total_ms；降级直答：retrieve_ms/degrade_ms/total_ms。
    """
    sources = dedup_sources(list(getattr(result, "sources", []) or []))
    charts = dedup_charts(list(getattr(result, "charts", []) or []))
    clarify = bool(getattr(result, "clarify", False))
    phase_times = dict(getattr(result, "phase_times", {}) or {})
    phase_times["total_ms"] = elapsed_ms
    return {
        "sources": [] if clarify else sources,
        "web_sources": [] if clarify else deepcopy(web_sources or []),
        "charts": [] if clarify else charts,
        "tool_called": bool(getattr(result, "tool_called", False)) and not clarify,
        "tool_name": "" if clarify else str(getattr(result, "first_tool", "") or ""),
        "tool_calls": deepcopy(list(getattr(result, "tool_trace", []) or [])),
        "rounds": int(getattr(result, "rounds", 0)),
        "hit_cap": bool(getattr(result, "hit_cap", False)),
        "phase_times": phase_times,
        "degraded": list(getattr(result, "degraded", []) or []),
        "clarify": clarify,
        "conversation_id": getattr(result, "conversation_id", None),
        "provider": provider,
        "trace_id": trace_id,
        "elapsed_ms": elapsed_ms,
        "schema_v": SCHEMA_V,
    }
