"""Qwen 视觉通道（docs/01 §3.1）：路由+描述一次调用与 JSON 容错解析。

v2.3 双通道：Qwen 看压缩预览，返回 {route, description}；解析失败兜底为
「整段当描述、route 按 describe」——最坏退化即 v2.2 现状行为，绝不因解析失败
让请求失败。槽位构造/失败→5031 的语义在 analyze.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bidqa.application.image import prompts


class VisionUnavailable(Exception):
    """视觉解析不可用（API 层转 5031；str = body 里的 source 细节）。"""


@dataclass
class RouteResult:
    route: str  # "describe" | "ocr"（解析不出来时按 describe）
    description: str


def route_and_describe(client, data_url: str) -> RouteResult:
    """一次调用同时拿到路由判断与描述（temperature 0.1，与 v2.2 描述口径一致）。

    上游异常原样抛出（analyze 统一包装为 VisionUnavailable）。
    """
    raw = str(client.chat(prompts.route_desc_messages(data_url), temperature=0.1)).strip()
    route, description = _parse_route_json(raw)
    return RouteResult(route=route, description=description)


def _parse_route_json(raw: str) -> tuple[str, str]:
    """取第一个 JSON 对象；任何不合规=整段当描述、route 按 describe（兜底即现状）。"""
    text = raw.strip()
    if text.startswith("```"):  # 剥掉 markdown 代码栅栏再试
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return "describe", raw
    if not isinstance(obj, dict):
        return "describe", raw
    route = obj.get("route") if obj.get("route") in ("describe", "ocr") else "describe"
    description = str(obj.get("description") or "").strip()
    if not description:  # 描述缺失=不可信输出，整段兜底
        return "describe", raw
    return route, description
