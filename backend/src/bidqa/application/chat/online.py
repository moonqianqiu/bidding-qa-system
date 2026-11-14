"""在线评测埋点编排（G4，docs/03 M4-3）：api 层一行调用；旁路绝不影响主流程。

从 api/chat/stream._online_record 与 api/conversations 点踩内联归位（v2.2 收口）：
采样判断与 payload 组装属业务编排，不属 HTTP 适配层。settings 由端点经 DI 传入
（不在旁路里直调 get_settings——测试隔离口径，2026-11-14 vision 同类缺陷定的规矩）。
"""

from __future__ import annotations

from typing import Any

from bidqa.eval.online import get_online, pick_sample


def record_chat_done(settings: Any, *, question: str, provider: str | None,
                     web_enabled: bool, thinking_enabled: bool, result: Any,
                     trace_id: str, elapsed_ms: int) -> None:
    """done 收尾埋点：行为全量免费；采样率>0 时抽中轮附带正文判分。

    result 为 agent.loop.LoopResult（鸭子类型，eval 不反向依赖 application）。
    """
    try:
        sample = None
        if pick_sample(settings.ONLINE_EVAL_SAMPLE_RATE):
            sample = {"question": question, "answer": result.answer,
                      "sources": list(result.sources or []), "trace_id": trace_id}
        get_online().record_done(
            provider=provider or "", web_enabled=web_enabled,
            thinking_enabled=thinking_enabled, rounds=int(result.rounds or 0),
            tool_name=str(result.first_tool or ""), hit_cap=bool(result.hit_cap),
            clarify=bool(result.clarify), degraded=bool(result.degraded),
            elapsed_ms=elapsed_ms, sample=sample, settings=settings)
    except Exception:
        pass  # 评测旁路失败不影响问答（docs/01：失败=等同不开启）


def record_feedback(settings: Any, *, trace_id: str, question: str,
                    answer: str, sources: list[dict]) -> None:
    """G4 点踩必评：忠实度单维（60s 去重在 online 层；旁路不抛错）。"""
    try:
        get_online().record_feedback_down(
            trace_id=trace_id, question=question, answer=answer,
            sources=list(sources), settings=settings)
    except Exception:
        pass  # 同上：失败=等同不开启
