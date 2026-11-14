"""G2 离线 RAGAS 评测（docs/02）：忠实度/相关性/上下文相关性三维 + 判分缓存 + 快照。

判分与被测异源（执行前强制校验，5032）；单题失败跳过不串批；
判分缓存按端点-模型分目录（风险表对策）；快照命名规范+滚动保留 100（G5）；
离线成本预算 <¥5 告警口径：单题 3 维 ≈ 数千 token。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bidqa.clients.llm import ChatClient
from bidqa.eval.agent_eval import (
    RESULTS_DIR,
    atomic_write_json,
    git_short_hash,
    judge_model_name,
)
from bidqa.eval.judge import JudgeFingerprintError, build_judge, validate_heterogeneous
from bidqa.rag.retrieve import Retriever

JUDGE_CACHE_DIR = RESULTS_DIR / "judge_cache"
SNAPSHOT_KEEP = 100  # G5 滚动保留

_DIM_PROMPTS = {
    "faithfulness": (
        "判断【回答】是否完全依据【材料】且无编造。完全忠实=1，有编造/越界=0。只输出一个数字。"),
    "relevancy": (
        "判断【回答】是否切题回答了【问题】。切题=1，跑题/拒答式空话=0。只输出一个数字。"),
    "context_relevancy": (
        "判断【材料】整体与【问题】的相关性。相关=1，不相关=0。只输出一个数字。"),
}


def _cache_path(fingerprint: tuple[str, str, str]) -> Path:
    endpoint, key, model = fingerprint
    safe = endpoint.replace("://", "_").replace("/", "_").replace(":", "_")
    JUDGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return JUDGE_CACHE_DIR / f"{safe}_{model}.json"


def _load_cache(fingerprint: tuple[str, str, str]) -> dict:
    path = _cache_path(fingerprint)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(fingerprint: tuple[str, str, str], cache: dict) -> None:
    atomic_write_json(_cache_path(fingerprint), cache)


def judge_dim(judge: ChatClient, dim: str, question: str, answer: str,
              context: str, cache: dict, cache_key: str) -> int | None:
    """单维判分（0/1）；判分输出非法 → None（该维不计入，绝不记 0——null≠0）。"""
    if cache_key in cache and dim in cache[cache_key]:
        value = cache[cache_key][dim]
        return None if value is None else int(value)
    try:
        raw = str(judge.chat([
            {"role": "system", "content": _DIM_PROMPTS[dim]},
            {"role": "user", "content": f"【问题】{question}\n【材料】{context[:1500]}\n【回答】{answer[:1200]}"},
        ], temperature=0.0)).strip()
        score = 1 if raw.startswith("1") else (0 if raw.startswith("0") else None)
    except Exception:
        score = None
    cache.setdefault(cache_key, {})[dim] = score
    return score


def run_rag_eval(golden_path: str, retriever: Retriever, chat: ChatClient,
                 settings: Any, judge: ChatClient | None = None) -> dict:
    """离线 RAG 评测主入口（生成用主槽位，判分用 judge 槽位，异源强制）。

    judge 可注入（测试用 fake）；缺省按 JUDGE_* 构建。
    """
    judge_fp = validate_heterogeneous(settings)  # 5032 先于任何成本发生
    judge = judge or build_judge(settings)
    cache = _load_cache(judge_fp)

    from bidqa.eval.agent_eval import load_golden
    cases = load_golden(golden_path)
    results, failed = [], []
    for case in cases:
        question = case["question"]
        try:
            t0 = time.perf_counter()
            retrieval = retriever.retrieve(question)
            # 自行计时：retrieval.elapsed_ms 是首次检索耗时，缓存命中时是旧值。
            retrieve_ms = int((time.perf_counter() - t0) * 1000)
            blocks = "\n\n".join(
                f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
                for i, h in enumerate(retrieval.hits))
            answer = chat.chat([
                {"role": "system", "content": "你是招投标领域问答助手。只依据【检索材料】作答；"
                                              "未覆盖回答「知识库未包含」。引用标注 [n]。"},
                {"role": "user", "content": f"【检索材料】\n{blocks}\n\n【问题】{question}"},
            ])
        except Exception as exc:
            failed.append({"id": case["id"], "question": question,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue

        cache_key = f"{question[:64]}|{answer[:64]}"
        dims: dict[str, int | None] = {}
        for dim in ("faithfulness", "relevancy", "context_relevancy"):
            dims[dim] = judge_dim(judge, dim, question, answer, blocks, cache, cache_key)
        scored = [v for v in dims.values() if v is not None]
        results.append({
            "id": case["id"], "question": question,
            "answer_head": answer[:80], "sources": len(retrieval.hits),
            "mode": retrieval.mode,
            "dims": dims,
            "score": round(sum(scored) / len(scored), 3) if scored else None,  # null=没测出
            "elapsed_ms": retrieve_ms,
        })
    _save_cache(judge_fp, cache)

    scored_results = [r for r in results if r["score"] is not None]
    summary = {
        "total": len(cases), "answered": len(results), "failed": len(failed),
        "avg_score": (round(sum(r["score"] for r in scored_results) / len(scored_results), 4)
                      if scored_results else None),  # null≠0
        "dims_avg": {
            dim: (round(sum(r["dims"][dim] for r in results if r["dims"][dim] is not None)
                        / max(1, sum(1 for r in results if r["dims"][dim] is not None)), 4))
            for dim in ("faithfulness", "relevancy", "context_relevancy")
        },
        "env": {"judge": list(judge_fp), "self_judged": False, "link": "internal"},
    }
    report = {"kind": "rag", "env": {"link": "internal", "judge": list(judge_fp),
                                     "self_judged": False},
              "summary": summary, "cases": results, "failed_cases": failed}
    _save_cache(judge_fp, cache)
    return report


def write_rag_snapshot(report: dict, settings: Any) -> Path:
    from bidqa.eval.agent_eval import snapshot_path
    path = snapshot_path(report["kind"], judge_model_name(settings))
    atomic_write_json(path, report)
    rolling_retention()
    return path


def rolling_retention(keep: int = SNAPSHOT_KEEP) -> int:
    """G5：快照滚动保留最近 N 个（按文件名排序=按日期命名规范）。"""
    files = sorted(p for p in RESULTS_DIR.glob("*.json") if p.is_file())
    removed = 0
    for path in files[:-keep] if len(files) > keep else []:
        path.unlink()
        removed += 1
    return removed


__all__ = [
    "JudgeFingerprintError", "run_rag_eval", "write_rag_snapshot",
    "rolling_retention", "judge_dim",
]
