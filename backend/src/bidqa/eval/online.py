"""G4 在线评测（docs/03 M4-3；docs/01 §B2 收尾事件处挂真实流量）。

- 行为事件全量免费：轮次/工具/hit_cap/延迟/降级/澄清等行为指标，不含问答正文；
- 采样判分：ONLINE_EVAL_SAMPLE_RATE>0 时按率抽样本轮（含正文+来源）做忠实度单维；
- 点踩必评：/api/feedback 收 down → 忠实度单维必评（60s 去重键，trace_id 优先）；
- 有界队列 + 双缓冲：事件先进内存缓冲（有界，满丢最旧），后台线程定期换缓冲落盘
  eval/results/online/{behavior|faithful}_YYYYMMDD.jsonl；
- 判分复用 G2 槽位与判分缓存（ragas.judge_dim），判分与被测异源——同指纹只告警跳过，
  绝不在请求路径抛错（在线评测是旁路，失败=等同不开启）；
- 默认 ONLINE_EVAL_ENABLED=False：全链路 no-op。单进程约定（docs/01 §6）。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("bidqa.eval.online")

FLUSH_INTERVAL = 5.0
BEHAVIOR_MAX = 2048  # 有界队列上限（满丢最旧）
DEDUP_WINDOW = 60.0
RESULTS_DIR = Path("eval/results/online")


def _today() -> str:
    return time.strftime("%Y%m%d")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.warning("在线评测落盘失败（等同不开启）: %s", path.name)


class OnlineEval:
    """在线评测单例：行为双缓冲 + 采样/点踩忠实度判分。所有方法绝不抛错。"""

    def __init__(self, results_dir: Path | None = None) -> None:
        self._dir = results_dir or RESULTS_DIR
        self._lock = threading.Lock()
        self._buffers: list[list[dict]] = [[], []]  # 双缓冲：生产者写一块，落盘排空另一块
        self._active = 0
        self._dropped = 0
        self._dedup: dict[str, float] = {}  # 去重键 -> monotonic ts
        self._stats = {"behavior": 0, "sampled": 0, "judged": 0, "judge_fail": 0,
                       "down_feedback": 0, "dedup_skipped": 0}
        self._closed = False
        threading.Thread(target=self._flush_loop, daemon=True, name="online-eval").start()

    # ── 行为事件（全量免费，无正文） ──

    def record_done(self, *, provider: str, web_enabled: bool, thinking_enabled: bool,
                    rounds: int, tool_name: str, hit_cap: bool, clarify: bool,
                    degraded: bool, elapsed_ms: int, sample: dict | None) -> None:
        """chat_stream 收尾调用；sample 非 None 表示本轮被抽中判分（含正文）。"""
        try:
            settings = _settings()
            if not settings.ONLINE_EVAL_ENABLED:
                return
            event = {"ts": time.strftime("%H:%M:%S"), "provider": provider,
                     "web": web_enabled, "thinking": thinking_enabled,
                     "rounds": rounds, "tool": tool_name, "hit_cap": hit_cap,
                     "clarify": clarify, "degraded": degraded, "elapsed_ms": elapsed_ms}
            with self._lock:
                buf = self._buffers[self._active]
                if len(buf) >= BEHAVIOR_MAX:
                    buf.pop(0)
                    self._dropped += 1
                buf.append(event)
                self._stats["behavior"] += 1
            if sample is not None:
                with self._lock:
                    self._stats["sampled"] += 1
                self._judge_async("sampling", sample)
        except Exception:
            pass  # 旁路绝不影响主流程

    # ── 点踩必评（60s 去重） ──

    def record_feedback_down(self, *, trace_id: str, question: str, answer: str,
                             sources: list[dict]) -> None:
        try:
            settings = _settings()
            if not settings.ONLINE_EVAL_ENABLED:
                return
            key = trace_id or f"{question[:64]}"
            now = time.monotonic()
            with self._lock:
                seen = self._dedup.get(key)
                if seen is not None and now - seen < DEDUP_WINDOW:
                    self._stats["dedup_skipped"] += 1
                    return
                self._dedup[key] = now
                if len(self._dedup) > 512:
                    for k in list(self._dedup)[:256]:
                        self._dedup.pop(k, None)
                self._stats["down_feedback"] += 1
            self._judge_async("feedback", {"question": question, "answer": answer,
                                           "sources": sources, "trace_id": trace_id})
        except Exception:
            pass

    # ── 忠实度单维（复用 G2 判分槽位 + 缓存） ──

    def _judge_async(self, trigger: str, sample: dict) -> None:
        threading.Thread(target=self._judge, args=(trigger, sample),
                         daemon=True, name="online-judge").start()

    def _judge(self, trigger: str, sample: dict) -> None:
        try:
            from bidqa.eval.ragas import judge_dim, JUDGE_CACHE_DIR
            settings = _settings()
            if not settings.has_judge:
                return
            from bidqa.eval.judge import chat_fingerprint, judge_fingerprint
            if chat_fingerprint(settings) == judge_fingerprint(settings):
                logger.warning("在线判分与主槽位同指纹，跳过（防自评）")  # 只告警不抛
                return
            judge = _judge_client(settings)
            cache: dict = {}
            context = "\n\n".join(f"[{i + 1}] {s.get('title', '')}\n{s.get('text', '')}"
                                  for i, s in enumerate(sample.get("sources", [])))
            cache_key = f"online|{trigger}|{sample.get('question', '')[:64]}|{sample.get('answer', '')[:64]}"
            score = judge_dim(judge, "faithfulness", sample.get("question", ""),
                              sample.get("answer", ""), context, cache, cache_key)
            row = {"ts": time.strftime("%H:%M:%S"), "trigger": trigger,
                   "trace_id": sample.get("trace_id", ""), "faithfulness": score}
            with self._lock:
                self._buffers[self._active].append(row)  # 判分结果与行为同管道落盘
                self._stats["judged" if score is not None else "judge_fail"] += 1
            if cache:
                _merge_judge_cache(settings, cache)
        except Exception:
            with self._lock:
                self._stats["judge_fail"] += 1

    # ── 双缓冲落盘 ──

    def _flush_loop(self) -> None:
        while not self._closed:
            time.sleep(FLUSH_INTERVAL)
            self._flush_once()

    def _flush_once(self) -> int:
        """换缓冲落盘一次（测试可直接调用）。返回落盘行数。"""
        try:
            with self._lock:
                old = self._active
                self._active = 1 - old  # 新事件改写另一块
                rows = self._buffers[old]  # 排空生产者刚写过的旧块
                self._buffers[old] = []
            _append_jsonl(self._dir / f"online_{_today()}.jsonl", rows)
            return len(rows)
        except Exception:
            return 0

    # ── H2 指标暴露 ──

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"queued": len(self._buffers[self._active]), "dropped": self._dropped,
                    **self._stats}


# ── 模块级单例与辅助 ──

_instance: OnlineEval | None = None
_settings_cache: tuple[float, Any] | None = None


def get_online() -> OnlineEval:
    global _instance
    if _instance is None:
        _instance = OnlineEval()
    return _instance


def _settings() -> Any:
    global _settings_cache
    now = time.monotonic()
    if _settings_cache is None or now - _settings_cache[0] > 5.0:
        from bidqa.config import get_settings
        _settings_cache = (now, get_settings())
    return _settings_cache[1]


def _judge_client(settings: Any) -> Any:
    from bidqa.eval.judge import build_judge
    return build_judge(settings)


def _merge_judge_cache(settings: Any, cache: dict) -> None:
    """把在线判分结果并回判分缓存（与离线共用同一缓存文件，离线跑批直接受益）。"""
    try:
        from bidqa.eval.agent_eval import atomic_write_json
        from bidqa.eval.judge import judge_fingerprint
        from bidqa.eval.ragas import _cache_path
        path = _cache_path(judge_fingerprint(settings))
        existing: dict = {}
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
        existing.update(cache)
        atomic_write_json(path, existing)
    except Exception:
        pass


def pick_sample(rate: float) -> bool:
    """采样决策（rate=0 恒 False；行为指标不受影响）。"""
    return rate > 0 and random.random() < rate
