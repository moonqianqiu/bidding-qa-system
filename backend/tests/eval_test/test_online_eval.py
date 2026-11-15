"""G4 在线评测单测：默认关全 no-op、采样率 0 不判分、点踩 60s 去重、忠实度单维、
有界队列满丢最旧、判分缓存回写。全程 fake judge（判分槽位收费，绝不真调）。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import bidqa.eval.online as online_mod
from bidqa.eval.online import OnlineEval, pick_sample


class FakeSettings:
    ONLINE_EVAL_ENABLED = True
    ONLINE_EVAL_SAMPLE_RATE = 0.0
    has_judge = True
    CHAT_BASE_URL = "https://chat.example"
    CHAT_API_KEY = "sk-chat-key-123456"
    CHAT_MODEL = "chat-model"
    JUDGE_BASE_URL = "https://judge.example"
    JUDGE_API_KEY = "sk-judge-key-654321"
    JUDGE_MODEL = "judge-model"


class FakeJudge:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def chat(self, messages: list[dict], temperature: float = 0.0) -> str:
        with self.lock:
            self.calls += 1
        return "1"  # 忠实


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: FakeSettings) -> None:
    online_mod._settings_cache = (time.monotonic(), settings)


def test_pick_sample_bounds():
    assert pick_sample(0.0) is False
    assert pick_sample(-1) is False
    assert pick_sample(1.0) is True


def test_disabled_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = FakeSettings()
    s.ONLINE_EVAL_ENABLED = False
    _use_settings(monkeypatch, s)
    oe = OnlineEval(results_dir=tmp_path)
    oe.record_done(provider="", web_enabled=False, thinking_enabled=False, rounds=1,
                   tool_name="search_knowledge", hit_cap=False, clarify=False,
                   degraded=False, elapsed_ms=100, sample=None)
    assert oe.stats()["behavior"] == 0
    assert oe._flush_once() == 0  # 关闭态不落盘


def test_behavior_recorded_without_judge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """采样率 0：行为全量记录（免费），绝不触发判分。"""
    _use_settings(monkeypatch, FakeSettings())
    oe = OnlineEval(results_dir=tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(oe, "_judge_async", lambda *a: calls.append(a))
    oe.record_done(provider="p", web_enabled=True, thinking_enabled=False, rounds=2,
                   tool_name="search_database", hit_cap=False, clarify=False,
                   degraded=False, elapsed_ms=1500, sample=None)
    stats = oe.stats()
    assert stats["behavior"] == 1
    assert stats["sampled"] == 0 and not calls  # 未抽中 → 无判分
    assert oe._flush_once() == 1
    rows = [json.loads(l) for l in
            (tmp_path / f"online_{time.strftime('%Y%m%d')}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["tool"] == "search_database"
    assert "question" not in rows[0] and "answer" not in rows[0]  # 行为事件不含正文


def test_sampled_triggers_judge_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """采样抽中 → 忠实度单维判分 + 缓存回写；判分结果落盘。"""
    _use_settings(monkeypatch, FakeSettings())
    monkeypatch.setattr(online_mod, "_judge_client", lambda s: FakeJudge())
    monkeypatch.setattr("bidqa.eval.ragas.JUDGE_CACHE_DIR", tmp_path / "jc")
    oe = OnlineEval(results_dir=tmp_path)
    oe._judge("sampling", {"question": "q", "answer": "a",
                           "sources": [{"title": "t", "text": "x"}], "trace_id": "tr1"})
    stats = oe.stats()
    assert stats["judged"] == 1
    assert oe._flush_once() == 1
    rows = [json.loads(l) for l in
            (tmp_path / f"online_{time.strftime('%Y%m%d')}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["faithfulness"] == 1
    assert rows[0]["trigger"] == "sampling"
    cache_files = list((tmp_path / "jc").glob("*.json"))
    assert cache_files, "判分结果应回写缓存（离线评测直接受益）"


def test_feedback_down_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """点踩必评 60s 去重：同 trace 重复点踩只判一次。"""
    _use_settings(monkeypatch, FakeSettings())
    oe = OnlineEval(results_dir=tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(oe, "_judge_async", lambda trigger, sample: calls.append(sample))
    for _ in range(3):
        oe.record_feedback_down(trace_id="dup1", question="q", answer="a", sources=[])
    assert oe.stats()["down_feedback"] == 1
    assert oe.stats()["dedup_skipped"] == 2
    assert len(calls) == 1


def test_bounded_queue_drops_oldest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_settings(monkeypatch, FakeSettings())
    oe = OnlineEval(results_dir=tmp_path)
    for i in range(online_mod.BEHAVIOR_MAX + 10):
        oe.record_done(provider="", web_enabled=False, thinking_enabled=False, rounds=i,
                       tool_name="", hit_cap=False, clarify=False, degraded=False,
                       elapsed_ms=1, sample=None)
    assert oe.stats()["dropped"] == 10  # 满丢最旧（有界队列）


def test_judge_starts_from_disk_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """在线判分先读离线判分缓存：同题同答缓存命中 = 0 次真调（审计 P0-4 重复烧钱）。"""
    _use_settings(monkeypatch, FakeSettings())
    monkeypatch.setattr("bidqa.eval.ragas.JUDGE_CACHE_DIR", tmp_path / "jc")
    from bidqa.eval.judge import judge_fingerprint
    from bidqa.eval.ragas import _cache_path

    path = _cache_path(judge_fingerprint(FakeSettings()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"online|sampling|q|a": {"faithfulness": 1}}),
                    encoding="utf-8")
    judge = FakeJudge()
    monkeypatch.setattr(online_mod, "_judge_client", lambda s: judge)
    oe = OnlineEval(results_dir=tmp_path)
    oe._judge("sampling", {"question": "q", "answer": "a", "sources": [],
                           "trace_id": "tr1"})
    assert judge.calls == 0  # 缓存命中，绝不真调（当前实现每次新开 cache dict=红）
    assert oe.stats()["judged"] == 1


def test_judge_none_result_never_persists_to_disk_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """判分失败（None）绝不落进磁盘缓存——merge 曾把 None 并进离线缓存文件。"""
    _use_settings(monkeypatch, FakeSettings())
    monkeypatch.setattr("bidqa.eval.ragas.JUDGE_CACHE_DIR", tmp_path / "jc")

    class Garbage:
        def chat(self, messages, temperature=0.0):
            return "我觉得不行"

    monkeypatch.setattr(online_mod, "_judge_client", lambda s: Garbage())
    oe = OnlineEval(results_dir=tmp_path)
    oe._judge("sampling", {"question": "q", "answer": "a", "sources": [],
                           "trace_id": "tr1"})
    assert oe.stats()["judge_fail"] == 1
    files = list((tmp_path / "jc").glob("*.json"))
    data = json.loads(files[0].read_text(encoding="utf-8")) if files else {}
    assert all(v is not None for entry in data.values() for v in entry.values())


def test_judge_client_singleton_per_fingerprint():
    s = FakeSettings()
    c1 = online_mod._judge_client(s)
    c2 = online_mod._judge_client(s)
    assert c1 is c2  # 当前实现每次新建判分客户端（连接不关）=红


def test_judge_async_bounded_queue_drops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """判分队列有界（硬约束 4：队列容量即并发上限）：满即丢并计数，单 worker 消费——
    当前每样本新起线程=无界（红）。"""
    _use_settings(monkeypatch, FakeSettings())
    oe = OnlineEval(results_dir=tmp_path)
    gate = threading.Event()
    processed: list[tuple] = []

    def slow_judge(trigger: str, sample: dict) -> None:
        gate.wait(2.0)
        processed.append((trigger, sample))

    monkeypatch.setattr(oe, "_judge", slow_judge)
    for i in range(online_mod.JUDGE_QUEUE_MAX + 3):
        oe._judge_async("sampling", {"i": i})
    assert oe.stats()["judge_dropped"] == 3  # 满即丢
    gate.set()
    deadline = time.time() + 2
    while len(processed) < online_mod.JUDGE_QUEUE_MAX and time.time() < deadline:
        time.sleep(0.01)
    assert len(processed) == online_mod.JUDGE_QUEUE_MAX  # 单 worker 全量消费
