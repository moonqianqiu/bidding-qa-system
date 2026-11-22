"""WP6 检索侧评测（docs/02 §0.1）：recall@k / MRR / nDCG@k 逐题聚合 + 快照。

零 LLM/零判分（离线免费口径，AGENTS 预算红线）：检索器由调用方注入（真实
Retriever 或 fake，鸭子类型只要求 .retrieve(question, top_k)）；EMBEDDING_* 已配置时
逐题 1 次嵌入调用（付费），未配置走本地 BM25 灾备。

匹配键（选型依据，勿随意改）：golden.ref_sources 存的是 Chunk.id =
uuid5(source_type|title|text)（database/qdrant.py，内容确定性生成、重复导入幂等），
也是 Qdrant point id；Hit.id = str(point.id)（rag/retrieve.py）——三者天然对齐，
eval/smoke.py 的 recalled 口径同键。Hit.ref 是给人看的引用标注（常为空）、
title 可重复，都不稳定，不作键。

汇总口径（null ≠ 0，AGENTS 硬约束 1）：指标纯函数层空 relevant → None（没测出），
summary.metrics_avg 只对非 None 值求均值、全 None 时记 None；检索返回空=0 是
真实失败信号，与「没测出」分开计数（not_measured / empty_ranked）。

语料注记（corpus_note）：随快照落档防误读外推，规模反映**运行时真实点数**
（调用方注入 corpus_count，eval 层不做 IO；NIGHT-026 起废除硬编码规模）——
点数不可得或空集合记「未确认/为空」，绝不冒充具体数字、不回落任何旧值。

集合与通道自描述（NIGHT-046）：快照 env 落 `collection`（本批实际指向的
Qdrant 集合名，与 factory.build_repo 同源；取不到记 None=不可得）、summary
落 `modes`（逐例 mode=hybrid/bm25_fallback 的实测例计数——通道口径以运行时
逐例 mode 为准，已有 mode 字段原样保留）；快照名在 golden 段后加 scope 段
「{集合}-{主通道}」，同日同金集跨集合/跨通道独立成档，不再靠人工归档对账
（NIGHT-040/042 教训）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bidqa.eval.agent_eval import load_golden, slug_segment
from bidqa.eval.retrieval_metrics import ndcg_at_k, recall_at_k, reciprocal_rank


def corpus_note(point_count: int | None) -> str:
    """语料注记（单入口收口，docs/02 §0.1）：规模如实随运行时点数变化。

    point_count = Qdrant 集合真实点数（调用方注入）；不可得（None）或空集合
    （0）时不冒充具体数字、不回落旧值（硬约束 1：取不到的数值绝不记 0 冒充）。
    """
    if point_count:  # 真实正数 → 注记如实含该数（语料增减后随快照变化）
        scale = f"当前 Qdrant 共 {point_count} 点"
    elif point_count == 0:
        scale = "当前 Qdrant 集合为空（无可用语料点）"  # 真实空读数，不给数字口径
    else:
        scale = "当前 Qdrant 点数不可用（未能读取集合点数）"
    return (f"{scale}，recall/MRR/nDCG 是当前语料上的观测值，"
            "结论不外推到全量语料；重新 ingest 后必须重测。")


def _metric_keys(ks: tuple[int, ...]) -> list[str]:
    return [f"recall@{k}" for k in ks] + [f"ndcg@{k}" for k in ks] + ["mrr"]


def run_retrieval_eval(golden_path: str, retriever: Any,
                       ks: tuple[int, ...] = (5, 10),
                       env: dict[str, Any] | None = None,
                       corpus_count: int | None = None) -> dict:
    """逐题检索 → 指标 → 均值；retriever 需提供 .retrieve(question, top_k)。

    ks 同时决定检索窗口（top_k=max(ks)）与指标项（每 k 各出 recall/ndcg，
    MRR 无 k 截断）；空 ks = 参数错误（Nothing to measure，当场抛）。
    corpus_count = 运行时 Qdrant 真实点数（调用方注入），决定 corpus_note 规模口径。
    summary.modes（NIGHT-046）：逐例 mode（hybrid/bm25_fallback）的实测例计数，
    通道口径随快照自描述；没测出（not_measured）的例未跑检索不计入，逐例 mode
    字段原样保留（兼容）。
    """
    if not ks:
        raise ValueError("ks 不能为空（至少一个 k）")
    top_k = max(ks)
    cases = load_golden(golden_path)
    results: list[dict[str, Any]] = []
    raw_values: dict[str, list[float]] = {key: [] for key in _metric_keys(ks)}
    for case in cases:
        refs = list(case.get("ref_sources") or [])
        if not refs:
            # 空 relevant：整题指标 None（没测出≠0），跳过检索省一次嵌入调用
            results.append({
                "id": case["id"], "question": case["question"],
                "refs": 0, "hits": None, "mode": None, "measured": False,
                "metrics": {key: None for key in _metric_keys(ks)},
            })
            continue
        result = retriever.retrieve(case["question"], top_k=top_k)
        ranked = [h.id for h in result.hits]
        metrics: dict[str, float | None] = {}
        for k in ks:
            metrics[f"recall@{k}"] = recall_at_k(ranked, refs, k)
            metrics[f"ndcg@{k}"] = ndcg_at_k(ranked, refs, k)
        metrics["mrr"] = reciprocal_rank(ranked, refs)
        for key, value in metrics.items():
            if value is not None:  # None=没测出，不入均值（绝不记 0）
                raw_values[key].append(value)
        results.append({
            "id": case["id"], "question": case["question"],
            "refs": len(refs), "hits": len(ranked), "mode": result.mode,
            "measured": True, "metrics": metrics,
        })
    mode_counts: dict[str, int] = {}  # NIGHT-046：通道口径自描述（只计实测例）
    for r in results:
        if r["measured"] and r["mode"]:
            mode_counts[r["mode"]] = mode_counts.get(r["mode"], 0) + 1
    summary = {
        "total": len(results),
        "measured": sum(1 for r in results if r["measured"]),
        "not_measured": sum(1 for r in results if not r["measured"]),  # 空 relevant
        "empty_ranked": sum(1 for r in results
                            if r["measured"] and r["hits"] == 0),  # 检索空=真实 0 分
        "modes": dict(sorted(mode_counts.items())),  # hybrid/bm25_fallback 逐例计数
        "metrics_avg": {
            key: round(sum(vals) / len(vals), 4) if vals else None
            for key, vals in raw_values.items()
        },
        "corpus_note": corpus_note(corpus_count),  # 语料注记随快照落档，防误读外推
    }
    return {
        "kind": "retrieval",
        "env": env if env is not None else {"link": "internal"},
        "summary": summary,
        "cases": results,
    }


def retrieval_scope_slug(report: dict) -> str:
    """retrieval 快照命名 scope 段（NIGHT-046，单入口）：{集合名}-{主通道}。

    集合 = env.collection（build_retrieval_env 注入；None=不可得 → 该半段省略）；
    主通道 = summary.modes 中实测例最多的 mode（并列取字典序首个，确定性）。
    两者皆不可得 → ""（退回旧命名，不带 scope 段）。文件名只是对账索引，权威
    口径在快照 env.collection / summary.modes 本体。
    """
    env = report.get("env") or {}
    collection = env.get("collection") if isinstance(env, dict) else None
    modes = (report.get("summary") or {}).get("modes") or {}
    dominant = ""
    if isinstance(modes, dict) and modes:
        dominant = max(sorted(modes), key=lambda m: modes[m])  # 并列取字典序首个
    parts = [slug_segment(collection), slug_segment(dominant)]
    return "-".join(p for p in parts if p)


def write_retrieval_snapshot(report: dict, settings: Any, *,
                             golden: str | Path | None = None) -> Path:
    """快照命名规范同 agent/rag（docs/01 §4.4）：{kind}_{日期}_{golden}_{git}_{judge}.json
    （NIGHT-036：golden 不可得退回旧四段名）。NIGHT-046：另落 scope 段
    「{集合}-{主通道}」（retrieval_scope_slug 单入口）——同日同金集跨集合/跨通道
    不互踩；agent/rag writer 不传 scope，命名不变。"""
    from bidqa.eval.agent_eval import atomic_write_json, judge_model_name, snapshot_path
    from bidqa.eval.ragas import rolling_retention

    path = snapshot_path(report["kind"], judge_model_name(settings), golden,
                         scope=retrieval_scope_slug(report))
    atomic_write_json(path, report)
    rolling_retention()  # 滚动保留与其他模式一致（P1-18）
    return path


def build_retrieval_env(settings: Any, corpus_count: int | None = None) -> dict[str, Any]:
    """快照 env：集合名（NIGHT-046）+ chat/embedding 指纹（脱敏，judge._fingerprint
    同款）+ 语料注记。

    retrieval 模式不调用 chat/judge——chat 指纹仅为批次环境留档；embedding 未
    配置（has_embedding=False）记 None = BM25 灾备通道，快照可区分两批。
    collection = settings.QDRANT_COLLECTION（本批评测实际指向的集合，与
    database.factory.build_repo 同源）；缺属性/空串记 None=不可得，绝不冒充
    默认集合名（硬约束 1）。通道口径以运行时逐例 mode 为准（summary.modes 落
    档），env 不重复记配置推导值。corpus_count 同 run_retrieval_eval：运行时
    真实点数，注记经 corpus_note 收口。
    """
    from bidqa.eval.judge import chat_fingerprint, embedding_fingerprint

    chat = list(chat_fingerprint(settings)) if getattr(settings, "has_chat", False) else None
    embedding = (list(embedding_fingerprint(settings))
                 if getattr(settings, "has_embedding", False) else None)
    collection = (str(getattr(settings, "QDRANT_COLLECTION", "") or "").strip()
                  or None)  # 空/缺=不可得，不冒充默认值（硬约束 1）
    return {"link": "internal", "collection": collection, "chat": chat,
            "embedding": embedding, "note": corpus_note(corpus_count)}
