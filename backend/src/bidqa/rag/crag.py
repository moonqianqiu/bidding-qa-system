"""E6 CRAG-lite 重检（docs/02 E6；阈值 M1 黄金集 P-R 定值，参数表 docs/01 §4.1）。

首位相关度 < 阈值 → 问题改写重检一次 → 仍低则诚实兜底（由调用方走「未包含」语义，
绝不编造）。改写走主模型一次调用；失败/改写无效即返回原结果（降级如实记录）。

只有**精排 relevance_score** 才是可比置信度：RRF 排名分 / bm25 点积分不表达
绝对相关度，retrieve.py 在这些路径把 hits[0].score 记为 None。本模块绝不凭
「不可用」的分数触发改写，也不把它写成 0.00。
"""

from __future__ import annotations

from typing import Any

from bidqa.clients.llm import ChatClient
from bidqa.rag.retrieve import RetrievalResult

CRAG_THRESHOLD = 0.35  # M1 定值（docs/01 §4.1 参数表）
CRAG_MAX_RETRY = 1

_REWRITE_PROMPT = (
    "你是检索查询改写器。把用户问题改写为更适合关键词检索的一句话查询："
    "保留领域术语，去掉口语与问句语气，不添加原问题没有的信息。只输出改写结果一行。"
)


def _fmt_score(score: float | None) -> str:
    """置信度不可用写「不可用」，绝不写 0.00（null≠0）。"""
    return "不可用" if score is None else f"{score:.2f}"


def needs_recheck(result: RetrievalResult, threshold: float = CRAG_THRESHOLD) -> bool:
    """仅当有可比的相关度且低于阈值才改写；score=None 不触发。"""
    return (bool(result.hits) and result.hits[0].score is not None
            and result.hits[0].score < threshold)


def rewrite_query(chat: ChatClient, question: str) -> str:
    try:
        rewritten = str(chat.chat([
            {"role": "system", "content": _REWRITE_PROMPT},
            {"role": "user", "content": question},
        ], temperature=0.0)).strip().splitlines()[0].strip()
    except Exception:
        return ""
    return rewritten if 0 < len(rewritten) <= 200 and rewritten != question else ""


def crag_retrieve(retriever: Any, chat: ChatClient | None, question: str,
                  result: RetrievalResult, threshold: float = CRAG_THRESHOLD) -> RetrievalResult:
    """低相关 → 改写重检一次；重检更优则采用；否则原样返回（诚实兜底交给调用方）。"""
    if not needs_recheck(result, threshold):
        return result
    if chat is None:
        result.degraded.append(
            f"crag_low_confidence({_fmt_score(result.hits[0].score)}<{threshold}): 无改写模型")
        return result
    rewritten = rewrite_query(chat, question)
    if not rewritten:
        result.degraded.append(
            f"crag_low_confidence({_fmt_score(result.hits[0].score)}): 改写不可用")
        return result
    second = retriever.retrieve(rewritten)
    second_score = second.hits[0].score if second.hits else None
    if second_score is not None and second_score > result.hits[0].score:
        second.degraded = list(result.degraded) + [
            f"crag_rewrite: 「{question}」→「{rewritten}」"
            f"{_fmt_score(result.hits[0].score)}→{_fmt_score(second_score)}"]
        return second
    result.degraded.append(
        f"crag_retry_kept_original: 改写重检未更优（{rewritten[:40]}）")
    return result
