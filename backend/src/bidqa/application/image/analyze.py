"""双通道路由编排（docs/01 §3.1，v2.3）：mode × 槽位 × Qwen 路由 → 最终描述。

行为矩阵（全部 fake 离线单测，见 tests/api_test/test_vision.py）：
- auto + OCR 未配置            → Qwen 描述（route_by=unavailable，= v2.2 现状，不算 degraded；
                                 单通道无路由可言，与路由结果无关）
- auto + 路由 describe         → Qwen 描述（route_by=auto）
- auto + 路由 ocr + OCR 成功   → OCR 原文（mode_used=ocr）
- auto + 路由 ocr + 失败/空/缺原件 → Qwen 描述兜底（degraded=true）
- describe（手动）             → 忽略 route，Qwen 描述（route_by=manual）
- ocr（手动）                  → 跳过 Qwen 最省钱；失败=5031 无兜底
约定：mode 合法性与 mode=ocr 的槽位/原件前置校验在 api 层（400/5031 口径）；
本模块假定入参已合法。VisionUnavailable → API 层 5031。
"""

from __future__ import annotations

from dataclasses import dataclass

from bidqa.application.image import ocr
from bidqa.application.image.qwen import RouteResult, VisionUnavailable, route_and_describe
from bidqa.clients.chat_factory import build_ocr, build_vision  # noqa: F401  # patch 缝（测试替身注入点）

OCR_TEXT_PREFIX = "【图片文字（OCR）】"  # 存入 RefStore 的来源标记（image_context_block 原样透传）


@dataclass
class AnalyzeResult:
    text: str        # 存入 RefStore 的最终描述（下游 image_context_block 原样消费）
    mode_used: str   # "describe" | "ocr"
    route_by: str    # "auto" | "manual" | "unavailable"
    degraded: bool = False


def analyze_image(preview: str, original: str | None, mode: str) -> AnalyzeResult:
    """双通道路由主入口。Qwen 是全端点的前提槽位（未配置=5031，与 v2.2 一致）。"""
    client = build_vision()
    if client is None:
        raise VisionUnavailable("vision: 视觉槽位未配置（VISION_*）")
    if mode == "ocr":
        return _ocr_only(original)
    try:
        rr = route_and_describe(client, preview)
    except Exception as exc:
        raise VisionUnavailable(f"vision: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not rr.description:
        raise VisionUnavailable("vision: 上游返回空描述")
    if mode == "describe":  # 手动通用描述：route 判断弃用
        return AnalyzeResult(rr.description, "describe", "manual")
    ocr_client = build_ocr()
    if ocr_client is None:  # auto + OCR 未配置 = v2.2 单通道现状，无路由可言（不算 degraded）
        return AnalyzeResult(rr.description, "describe", "unavailable")
    if rr.route != "ocr":
        return AnalyzeResult(rr.description, "describe", "auto")
    return _try_ocr(ocr_client, original, rr)


def _try_ocr(client, original: str | None, rr: RouteResult) -> AnalyzeResult:
    """auto + 路由 ocr：OCR 读高分辨率原件；任何不成功都用 Qwen 描述兜底。"""
    if not original:  # 前端恒传；缺失=请求不完整，尽力兜底不失败
        return AnalyzeResult(rr.description, "describe", "auto", degraded=True)
    try:
        text = ocr.ocr_extract(client, original)
    except Exception:  # OCR 半路失败 ≠ 整请求失败：描述在手，标注降级
        return AnalyzeResult(rr.description, "describe", "auto", degraded=True)
    if not text:
        return AnalyzeResult(rr.description, "describe", "auto", degraded=True)
    return AnalyzeResult(f"{OCR_TEXT_PREFIX}\n{text}", "ocr", "auto")


def _ocr_only(original: str | None) -> AnalyzeResult:
    """手动按文档解析：不调 Qwen（最省钱）；失败诚实 5031（api 层已前置校验）。"""
    client = build_ocr()
    if client is None:
        raise VisionUnavailable("ocr: OCR 槽位未配置（OCR_*）")
    if not original:
        raise VisionUnavailable("ocr: mode=ocr 需要 original 字段（高分辨率原件）")
    try:
        text = ocr.ocr_extract(client, original)
    except Exception as exc:
        raise VisionUnavailable(f"ocr: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not text:
        raise VisionUnavailable("ocr: 上游返回空文本")
    return AnalyzeResult(f"{OCR_TEXT_PREFIX}\n{text}", "ocr", "manual")
