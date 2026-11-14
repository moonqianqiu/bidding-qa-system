"""E6 CRAG-lite（阈值可配/改写重检一次/仍低诚实兜底/降级如实记录）。"""

from __future__ import annotations

from bidqa.rag.crag import (
    CRAG_THRESHOLD, _fmt_score, crag_retrieve, needs_recheck, rewrite_query,
)
from bidqa.rag.retrieve import Hit, RetrievalResult


class FakeChat:
    def __init__(self, reply: str = "单一来源采购 适用条件"):
        self.reply = reply
        self.calls = 0

    def chat(self, messages, temperature=0.3):
        self.calls += 1
        return self.reply


class FakeRetriever:
    def __init__(self, scores_by_query: dict[str, list[float | None]]):
        self.scores = scores_by_query

    def retrieve(self, question: str, top_k: int = 5) -> RetrievalResult:
        scores = self.scores.get(question, [0.1])
        hits = [Hit(id=f"c{i}", score=s, source_type="法规", title="T", text="x")
                for i, s in enumerate(scores)]
        return RetrievalResult(hits=hits, kind="concept", rrf_k=90, mode="hybrid",
                               elapsed_ms=1)


def make_result(score: float | None) -> RetrievalResult:
    hit = Hit(id="c0", score=score, source_type="法规", title="T", text="x")
    return RetrievalResult(hits=[hit], kind="concept", rrf_k=90, mode="hybrid", elapsed_ms=1)


def test_needs_recheck_threshold() -> None:
    assert needs_recheck(make_result(0.30)) is True   # < 0.35
    assert needs_recheck(make_result(0.35)) is False
    assert needs_recheck(make_result(0.80)) is False
    empty = RetrievalResult(hits=[], kind="concept", rrf_k=90, mode="hybrid", elapsed_ms=1)
    assert needs_recheck(empty) is False


def test_rewrite_query_output_cleaned() -> None:
    chat = FakeChat("第1行改写\n第2行不该出现")
    assert rewrite_query(chat, "你好我想问一下那个空调的事") == "第1行改写"
    assert rewrite_query(FakeChat("原问题原样返回"), "原问题原样返回") == ""  # 相同=无效
    assert rewrite_query(FakeChat(""), "q") == ""


def test_crag_rewrites_and_adopts_better() -> None:
    retriever = FakeRetriever({"单一来源采购 适用条件": [0.85]})
    result = make_result(0.20)
    out = crag_retrieve(retriever, FakeChat(), "原问题", result)
    assert out.hits[0].score == 0.85
    assert any(d.startswith("crag_rewrite") for d in out.degraded)


def test_crag_keeps_original_when_retry_not_better() -> None:
    retriever = FakeRetriever({"改写后": [0.25]})  # 重检仍低且未更优
    result = make_result(0.30)
    out = crag_retrieve(retriever, FakeChat("改写后"), "原问题", result)
    assert out.hits[0].score == 0.30  # 诚实保留原结果，兜底语义交给调用方
    assert any(d.startswith("crag_retry_kept_original") for d in out.degraded)


def test_crag_no_chat_records_degraded() -> None:
    out = crag_retrieve(FakeRetriever({}), None, "q", make_result(0.2))
    assert any("crag_low_confidence" in d for d in out.degraded)


def test_crag_skips_high_confidence() -> None:
    chat = FakeChat()
    retriever = FakeRetriever({"改写": [0.9]})
    out = crag_retrieve(retriever, chat, "q", make_result(0.9))
    assert chat.calls == 0  # 高相关不消耗改写调用
    assert out.hits[0].score == 0.9


def test_threshold_is_m1_value() -> None:
    assert CRAG_THRESHOLD == 0.35  # docs/01 §4.1 参数表


# ── score 口径收口：None 不触发改写、不写成 0.00 ──

def test_needs_recheck_none_score_does_not_trigger() -> None:
    """P0 核心：没有可比相关度时不凭 RRF 分误触发改写。"""
    result = make_result(None)
    assert needs_recheck(result) is False
    chat = FakeChat()
    out = crag_retrieve(FakeRetriever({"改写": [0.9]}), chat, "q", result)
    assert out is result and chat.calls == 0


def test_fmt_score_none_is_not_zero() -> None:
    assert _fmt_score(None) == "不可用"
    assert "0.00" not in _fmt_score(0.2)


def test_crag_degraded_text_no_fake_zero() -> None:
    out = crag_retrieve(FakeRetriever({}), None, "q", make_result(0.2))
    text = " ".join(out.degraded)
    assert "crag_low_confidence" in text
    assert "0.20" in text and "0.00" not in text


def test_crag_adopts_second_with_valid_score() -> None:
    out = crag_retrieve(FakeRetriever({"单一来源采购 适用条件": [0.85]}),
                        FakeChat(), "原问题", make_result(0.20))
    assert out.hits[0].score == 0.85


def test_crag_skips_when_second_score_is_none() -> None:
    """重检结果无可比相关度：保留原结果，绝不把 None 当 0 比较。"""
    out = crag_retrieve(FakeRetriever({"单一来源采购 适用条件": [None]}),
                        FakeChat(), "原问题", make_result(0.20))
    assert out.hits[0].score == 0.20
    assert any(d.startswith("crag_retry_kept_original") for d in out.degraded)
