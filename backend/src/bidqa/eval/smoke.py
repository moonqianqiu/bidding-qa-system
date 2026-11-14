"""黄金集 smoke 评测（docs/02 G1；M1-10）。

mode=smoke：只查来源存在+可定位（不判分、不调 LLM）——ref_sources 全部能在
集合中按 id 定位 → PASS。内部函数层直调（docs/01 §3.6，不经 HTTP）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from bidqa.database.qdrant import QdrantRepo
from bidqa.rag.retrieve import Retriever


def run_smoke(repo: QdrantRepo, retriever: Retriever, golden_path: str,
              top_k: int = 5) -> dict:
    cases = [json.loads(l) for l in Path(golden_path).read_text(encoding="utf-8").splitlines()
             if l.strip()]
    existing = repo.existing_ids([rid for c in cases for rid in c.get("ref_sources", [])])
    results = []
    for case in cases:
        refs = case.get("ref_sources", [])
        located = [rid for rid in refs if rid in existing]
        t0 = time.perf_counter()
        result = retriever.retrieve(case["question"], top_k=top_k)
        # 自行计时：result.elapsed_ms 是首次（未命中缓存那次）检索的耗时，
        # 缓存命中时随整个结果对象原样返回旧值，不能当本次延迟用。
        retrieve_ms = int((time.perf_counter() - t0) * 1000)
        hit_ids = {h.id for h in result.hits}
        recalled = [rid for rid in refs if rid in hit_ids]
        results.append({
            "id": case["id"], "question": case["question"],
            "refs": len(refs), "located": len(located), "recalled": len(recalled),
            "pass": bool(refs) and len(located) == len(refs),
            "mode": result.mode, "elapsed_ms": retrieve_ms,
        })
    passed = sum(1 for r in results if r["pass"])
    located_total = sum(r["located"] for r in results)
    refs_total = sum(r["refs"] for r in results)
    return {
        "mode": "smoke",
        "cases": results,
        "summary": {
            "total": len(results), "passed": passed,
            "located_rate": round(located_total / refs_total, 4) if refs_total else None,
            "retrieval_hit_rate": round(
                sum(r["recalled"] for r in results) / refs_total, 4) if refs_total else None,
            "latency_p95_ms": sorted(r["elapsed_ms"] for r in results)[
                max(0, int(len(results) * 0.95) - 1)] if results else None,
        },
    }
