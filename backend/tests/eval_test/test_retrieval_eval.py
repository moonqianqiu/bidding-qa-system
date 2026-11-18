"""WP6：run_retrieval_eval 聚合——fake retriever 注入，零真实调用（embedding API 付费）。

覆盖：聚合均值精确值、字段齐全、空 relevant=None 不入均值、检索空=0 真实信号
单独计数、重复 id 去重、top_k=max(ks) 传参、env 透传。
"""

import json

import pytest

from bidqa.eval.retrieval_eval import CORPUS_NOTE, run_retrieval_eval
from bidqa.rag.retrieve import Hit, RetrievalResult


class FakeRetriever:
    """脚本化 fake：question → (hits, mode)；记录调用供断言（不触任何 API）。"""

    def __init__(self, script: dict[str, list[Hit]], mode: str = "bm25_fallback"):
        self.script = script
        self.mode = mode
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, question: str, top_k: int = 5) -> RetrievalResult:
        self.calls.append((question, top_k))
        return RetrievalResult(hits=self.script[question], kind="faq", rrf_k=8,
                               mode=self.mode, elapsed_ms=1)


def _hit(rid: str) -> Hit:
    return Hit(id=rid, score=None, source_type="FAQ", title=f"t-{rid}", text=f"x-{rid}")


def _write_golden(tmp_path, cases: list[dict]) -> str:
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases),
                    encoding="utf-8")
    return str(path)


def _golden_cases() -> list[dict]:
    q1, q2, q4 = "问题一", "问题二", "问题四"
    return [
        {"id": "c1", "question": q1, "ref_sources": ["r1"]},
        {"id": "c2", "question": q2, "ref_sources": ["r1", "r2"]},
        {"id": "c3", "question": "问题三（无期望来源）", "ref_sources": []},
        {"id": "c4", "question": q4, "ref_sources": ["r1"]},
    ]


def _script() -> dict[str, list[Hit]]:
    return {
        "问题一": [_hit("r1"), _hit("x9")],
        "问题二": [_hit("x9"), _hit("r1")],
        "问题四": [],  # 检索空=真实 0 分
    }


def test_run_retrieval_eval_aggregates_exact(tmp_path):
    retriever = FakeRetriever(_script())
    golden = _write_golden(tmp_path, _golden_cases())
    report = run_retrieval_eval(golden, retriever, ks=(1, 2))

    # 聚合均值（手算精确值）：c1 全中 / c2 半中（期望在第 2 位）/ c3 没测出 / c4 检索空
    assert report["summary"]["metrics_avg"] == {
        "recall@1": 0.3333,   # (1+0+0)/3
        "recall@2": 0.5,      # (1+0.5+0)/3
        "ndcg@1": 0.3333,
        "ndcg@2": 0.4623,     # (1 + D2/(1+D2) + 0)/3
        "mrr": 0.5,           # (1+0.5+0)/3
    }
    assert report["summary"]["total"] == 4
    assert report["summary"]["measured"] == 3
    assert report["summary"]["not_measured"] == 1  # c3 空 relevant=没测出
    assert report["summary"]["empty_ranked"] == 1  # c4 检索空=真实 0 分，分开计数
    assert report["summary"]["corpus_note"] == CORPUS_NOTE
    assert "75" in report["summary"]["corpus_note"]

    # 检索窗口 = max(ks)；空 relevant 跳过检索（省一次嵌入调用）
    assert retriever.calls == [("问题一", 2), ("问题二", 2), ("问题四", 2)]

    # 逐题字段齐全：c1 / c2 / c4
    c1, c2, c4 = report["cases"][0], report["cases"][1], report["cases"][3]
    assert (c1["metrics"]["recall@1"], c1["metrics"]["recall@2"],
            c1["metrics"]["ndcg@2"], c1["metrics"]["mrr"]) == (1.0, 1.0, 1.0, 1.0)
    assert c2["metrics"]["recall@2"] == pytest.approx(0.5)
    assert c2["metrics"]["ndcg@2"] == pytest.approx(0.6309297535714575 / 1.6309297535714574)
    assert c4["metrics"] == {"recall@1": 0.0, "recall@2": 0.0,
                             "ndcg@1": 0.0, "ndcg@2": 0.0, "mrr": 0.0}
    for case in (c1, c2, c4):
        assert case["mode"] == "bm25_fallback" and case["measured"] is True
    assert c1["hits"] == 2 and c2["hits"] == 2
    assert c4["hits"] == 0

    # c3：整题 None（没测出≠0）
    c3 = report["cases"][2]
    assert c3["measured"] is False and c3["refs"] == 0
    assert set(c3["metrics"].values()) == {None}

    # 报告结构：kind + env 缺省
    assert report["kind"] == "retrieval"
    assert report["env"] == {"link": "internal"}


def test_run_retrieval_eval_duplicate_hits_dedupe(tmp_path):
    """重复 id 去重保序：[r1,r1,r2] 截 2 仍含 r2（不去重 → recall@2=0，断言可红）。"""
    retriever = FakeRetriever({"问题": [_hit("r1"), _hit("r1"), _hit("r2")]})
    golden = _write_golden(tmp_path, [{"id": "c1", "question": "问题",
                                       "ref_sources": ["r2"]}])
    report = run_retrieval_eval(golden, retriever, ks=(2, 2))
    case = report["cases"][0]
    assert case["metrics"]["recall@2"] == 1.0
    assert case["metrics"]["mrr"] == 0.5  # r2 首现于去重后第 2 位


def test_run_retrieval_eval_env_passthrough(tmp_path):
    golden = _write_golden(tmp_path, [{"id": "c1", "question": "问题一",
                                       "ref_sources": ["r1"]}])
    retriever = FakeRetriever({"问题一": [_hit("r1")]})
    report = run_retrieval_eval(golden, retriever, ks=(5,),
                                env={"chat": ["ep", "k", "m"], "note": "n"})
    assert report["env"] == {"chat": ["ep", "k", "m"], "note": "n"}


def test_run_retrieval_eval_empty_ks_raises(tmp_path):
    golden = _write_golden(tmp_path, [{"id": "c1", "question": "问题一",
                                       "ref_sources": ["r1"]}])
    with pytest.raises(ValueError):
        run_retrieval_eval(golden, FakeRetriever({"问题一": []}), ks=())


def test_run_retrieval_eval_default_ks_emit_5_and_10(tmp_path):
    golden = _write_golden(tmp_path, [{"id": "c1", "question": "问题一",
                                       "ref_sources": ["r1"]}])
    retriever = FakeRetriever({"问题一": [_hit("r1")]})
    report = run_retrieval_eval(golden, retriever)
    assert set(report["summary"]["metrics_avg"]) == {
        "recall@5", "recall@10", "ndcg@5", "ndcg@10", "mrr"}
    assert retriever.calls == [("问题一", 10)]  # 检索窗口取 max(ks)
