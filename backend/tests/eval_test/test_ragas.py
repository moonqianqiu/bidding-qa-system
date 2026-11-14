"""G2 离线评测（异源指纹 5032/三维判分/判分缓存/快照滚动保留 null≠0）。"""

from __future__ import annotations

import json

import pytest

from bidqa.eval import ragas
from bidqa.eval.judge import JudgeFingerprintError, validate_heterogeneous
from bidqa.rag.retrieve import Hit, RetrievalResult


class FakeSettings:
    CHAT_BASE_URL = "https://main.example.com/v1"
    CHAT_API_KEY = "sk-main-key-123456"
    CHAT_MODEL = "main-model"
    JUDGE_BASE_URL = "https://judge.example.com/v1"
    JUDGE_API_KEY = "sk-judge-key-654321"
    JUDGE_MODEL = "judge-model"
    has_judge = True


def test_fingerprint_identical_rejected_5032() -> None:
    """端点+凭据+模型完全相同 → 5032 拒跑（防自评作弊）。"""
    s = FakeSettings()
    s.JUDGE_BASE_URL, s.JUDGE_API_KEY, s.JUDGE_MODEL = s.CHAT_BASE_URL, s.CHAT_API_KEY, s.CHAT_MODEL
    with pytest.raises(JudgeFingerprintError):
        validate_heterogeneous(s)


def test_fingerprint_same_endpoint_warns_but_passes() -> None:
    s = FakeSettings()
    s.JUDGE_API_KEY = "sk-other-key-999999"  # 仅端点相同 → 告警放行
    fp = validate_heterogeneous(s)
    assert fp[0] == s.JUDGE_BASE_URL


def test_fingerprint_no_judge_rejected() -> None:
    s = FakeSettings()
    s.has_judge = False
    with pytest.raises(JudgeFingerprintError):
        validate_heterogeneous(s)


class FakeJudge:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, temperature=0.0):
        self.calls += 1
        return "1"


class FakeChat:
    def chat(self, messages, temperature=0.3):
        return "依据[1]，可以再次提出异议。"


class FakeRetriever:
    def retrieve(self, question, top_k=5):
        hit = Hit(id="c1", score=0.9, source_type="FAQ", title="T", text="材料内容")
        return RetrievalResult(hits=[hit], kind="concept", rrf_k=90, mode="hybrid",
                               elapsed_ms=3)


GOLDEN = '{"id": "r1", "query_type": "rag_flow", "question": "可以再次异议吗？", "ref_sources": []}\n'


def test_run_rag_eval_dims_and_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ragas, "RESULTS_DIR", tmp_path / "eval" / "results")
    monkeypatch.setattr(ragas, "JUDGE_CACHE_DIR",
                        tmp_path / "eval" / "results" / "judge_cache")
    golden = tmp_path / "g.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")
    judge = FakeJudge()
    report = ragas.run_rag_eval(str(golden), FakeRetriever(), FakeChat(), FakeSettings(), judge=judge)
    assert report["kind"] == "rag"
    assert report["summary"]["avg_score"] == 1.0
    assert report["cases"][0]["dims"]["faithfulness"] == 1
    assert report["summary"]["env"]["self_judged"] is False

    # 判分缓存：同题同答第二次判分 0 次调用
    cache_files = list(ragas.JUDGE_CACHE_DIR.glob("*.json"))
    assert cache_files, "判分缓存应落盘"
    report2 = ragas.run_rag_eval(str(golden), FakeRetriever(), FakeChat(), FakeSettings(), judge=judge)
    assert report2["summary"]["avg_score"] == 1.0
    assert judge.calls == 3  # 3 维只判一次，第二轮全命中缓存


def test_judge_dim_invalid_output_is_null_not_zero(tmp_path, monkeypatch) -> None:
    """判分输出非法 → None（null=没测出 ≠ 0=得零分，docs/01 §4.4）。"""
    class Garbage:
        def chat(self, messages, temperature=0.0):
            return "我觉得还行"

    dim = ragas.judge_dim(Garbage(), "faithfulness", "q", "a", "ctx", {}, "k")
    assert dim is None


def test_rolling_retention_keeps_100(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(ragas, "RESULTS_DIR", results)
    for i in range(105):
        (results / f"rag_20261101_{i:04d}_judge.json").write_text("{}", encoding="utf-8")
    removed = ragas.rolling_retention()
    assert removed == 5
    assert len(list(results.glob("*.json"))) == 100
