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


def test_judge_dim_none_not_cached_and_rejudged() -> None:
    """None 不入缓存（硬约束 2 只缓存成功结果）：判分失败后恢复时同题必须重判——
    缓存 None 曾让该题此后永久 null 不再判（审计 P0-4 污染 G2）。"""
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0
            self.ok = False

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            return "1" if self.ok else "我觉得不行"

    judge = Flaky()
    cache: dict = {}
    assert ragas.judge_dim(judge, "faithfulness", "q", "a", "ctx", cache, "k") is None
    assert cache == {}  # None 不写缓存
    judge.ok = True
    assert ragas.judge_dim(judge, "faithfulness", "q", "a", "ctx", cache, "k") == 1
    assert judge.calls == 2  # 第二次必须真调（None 曾被缓存 → 不再请求）
    assert ragas.judge_dim(judge, "faithfulness", "q", "a", "ctx", cache, "k") == 1
    assert judge.calls == 2  # 成功结果才进缓存


def test_judge_dim_legacy_none_cache_entry_is_ignored() -> None:
    """旧缓存文件已被 None 污染的条目：命中分支忽略并重判（P0-4 收口）。
    新结果落内容寻址键（P2-17），旧 None 条目不再被读取。"""
    cache: dict = {"k": {"faithfulness": None}}

    class Once:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            return "1"

    j = Once()
    assert ragas.judge_dim(j, "faithfulness", "q", "a", "ctx", cache, "k") == 1
    assert j.calls == 1  # 历史 None 条目被忽略并真判
    assert cache["k"]["faithfulness"] is None  # 旧条目原样保留（不再参与命中）
    assert any(v.get("faithfulness") == 1 for key, v in cache.items() if key != "k")


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


def test_dims_avg_none_when_all_dims_unmeasured(tmp_path, monkeypatch) -> None:
    """P1-17（审计）：dims_avg 曾把空集记 0.0（sum([])/max(1,0)）——「没测出」
    绝不记 0（硬约束 10），与同函数 avg_score 的 None 口径一致。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ragas, "RESULTS_DIR", tmp_path / "eval" / "results")
    monkeypatch.setattr(ragas, "JUDGE_CACHE_DIR",
                        tmp_path / "eval" / "results" / "judge_cache")
    golden = tmp_path / "g.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")

    class Garbage:
        def chat(self, messages, temperature=0.0):
            return "我觉得还行"  # 非法判分输出 → 全维 None

    report = ragas.run_rag_eval(str(golden), FakeRetriever(), FakeChat(),
                                FakeSettings(), judge=Garbage())
    dims_avg = report["summary"]["dims_avg"]
    assert all(v is None for v in dims_avg.values()), dims_avg  # None≠0（原全 0.0=红）
    assert report["summary"]["avg_score"] is None


def test_judge_cache_key_distinguishes_context(tmp_path, monkeypatch) -> None:
    """P2-17（审计）：缓存键曾只含 question+answer 前缀——同问同答不同材料
    （faithfulness 判分依赖 context）命中同一缓存=判分失真。"""
    judge = FakeJudge()
    cache: dict = {}
    ragas.judge_dim(judge, "faithfulness", "同一问题", "同一回答", "材料甲", cache, "k")
    ragas.judge_dim(judge, "faithfulness", "同一问题", "同一回答", "材料乙", cache, "k")
    assert judge.calls == 2  # 不同 context 必须各自真判（原同键=1，红）


def test_rag_snapshot_has_no_answer_text(tmp_path, monkeypatch) -> None:
    """P2-18（§4.4 明令快照不落回答正文）：answer_head 曾把正文前 80 字写进快照
    并经 /api/eval/runs/{name} 原样返回。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ragas, "RESULTS_DIR", tmp_path / "eval" / "results")
    monkeypatch.setattr(ragas, "JUDGE_CACHE_DIR",
                        tmp_path / "eval" / "results" / "judge_cache")
    golden = tmp_path / "g.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")
    report = ragas.run_rag_eval(str(golden), FakeRetriever(), FakeChat(),
                                FakeSettings(), judge=FakeJudge())
    assert all("answer_head" not in c for c in report["cases"])


def test_rolling_retention_by_mtime_not_filename(tmp_path, monkeypatch) -> None:
    """P1-18（审计）：按文件名字典序删除——agent_ < rag_ < smoke_，超限时先删光
    整类 agent 快照。改按 mtime 排序（最旧先删）。"""
    import os

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(ragas, "RESULTS_DIR", results)
    for i in range(101):  # 101 个 agent 快照（mtime 递增）
        f = results / f"agent_20261101_{i:04d}_judge.json"
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    removed = ragas.rolling_retention(keep=100)
    assert removed == 1
    survivors = sorted(p.name for p in results.glob("agent_*.json"))
    assert len(survivors) == 100  # 按 mtime 保留最新（按名删会先删光 agent_ 前缀整类）
    assert survivors[0] == "agent_20261101_0001_judge.json"  # 最旧的被删
