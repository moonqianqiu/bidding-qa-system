"""WP6：run_retrieval_eval 聚合——fake retriever 注入，零真实调用（embedding API 付费）。

覆盖：聚合均值精确值、字段齐全、空 relevant=None 不入均值、检索空=0 真实信号
单独计数、重复 id 去重、top_k=max(ks) 传参、env 透传、语料注记随运行时真实
点数变化（NIGHT-026：不可得/空集合不冒充数字、不回落硬编码旧值）。
"""

import json

import pytest

from bidqa.eval.retrieval_eval import (
    build_retrieval_env,
    corpus_note,
    run_retrieval_eval,
)
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
    # 缺省（调用方未注入点数）= 点数不可得口径：不冒充数字、不回落硬编码旧值
    assert report["summary"]["corpus_note"] == corpus_note(None)
    assert "75" not in report["summary"]["corpus_note"]

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


def test_corpus_note_reflects_runtime_point_count(tmp_path):
    """NIGHT-026：语料注记随运行时真实点数变化——点数 793 时注记如实含 793。

    变异自证：若注记退回硬编码规模（旧值），"793" 断言必红（已实测）。
    """
    golden = _write_golden(tmp_path, [{"id": "c1", "question": "问题一",
                                       "ref_sources": ["r1"]}])
    retriever = FakeRetriever({"问题一": [_hit("r1")]})
    report = run_retrieval_eval(golden, retriever, ks=(5,), corpus_count=793)
    note = report["summary"]["corpus_note"]
    assert "793" in note   # 真实点数如实入注记（语料增减后随快照变化）
    assert "75" not in note  # 绝不回落硬编码旧值


def test_corpus_note_no_count_never_invents_number():
    """点数不可得（None）或空集合（0）：不冒充具体数字、不回落 75（硬约束 1）。"""
    for unavailable in (None, 0):
        note = corpus_note(unavailable)
        assert "75" not in note
        assert not any(ch.isdigit() for ch in note)  # 全注记无任何数字 = 不冒充规模
    assert corpus_note(0) != corpus_note(None)  # 空集合与不可得口径可区分、均不虚报
    assert "793" in corpus_note(793)
    assert corpus_note(1) != corpus_note(2)  # 不同真实点数 → 不同注记


def test_build_retrieval_env_note_reflects_count():
    """env.note 与 summary.corpus_note 同经 corpus_note 单入口收口（点数注入）。"""

    class _Settings:  # getattr 守卫：无 chat/embedding → 指纹槽 None（不触网络）
        has_chat = False
        has_embedding = False

    env = build_retrieval_env(_Settings(), corpus_count=793)
    assert env["note"] == corpus_note(793)
    assert "793" in env["note"]
    assert {"link", "chat", "embedding"} <= set(env)  # 指纹槽结构不变


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


def test_write_retrieval_snapshot_golden_segment(tmp_path, monkeypatch):
    """NIGHT-036：retrieval 快照名含 golden 段——同日同模式两金集独立文件；
    落盘隔离到 tmp（AGENTS 红线），不跑真实评测。"""
    from bidqa.eval.retrieval_eval import write_retrieval_snapshot

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr("bidqa.eval.agent_eval.RESULTS_DIR", results)
    monkeypatch.setattr("bidqa.eval.ragas.RESULTS_DIR", results)
    monkeypatch.setattr("bidqa.eval.agent_eval.git_short_hash", lambda: "abc1234")
    report = {"kind": "retrieval", "env": {"link": "internal"},
              "summary": {"metrics_avg": {}}, "cases": []}

    class S:
        JUDGE_MODEL = ""

    path = write_retrieval_snapshot(report, S(),
                                    golden=str(tmp_path / "rag_v0.jsonl"))
    assert "rag_v0" in path.name and path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "retrieval"
