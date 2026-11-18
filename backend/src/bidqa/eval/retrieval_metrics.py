"""检索排序指标（WP6，docs/02 §0.1）：recall@k / MRR / nDCG@k，二值相关。

纯函数、零 I/O、零模型调用——离线评测免费口径（AGENTS 预算红线）。
`ranked` = 检索返回的 id 有序列表（最相关在前）；`relevant` = 黄金集期望来源集合。

口径（null ≠ 0，AGENTS 硬约束 1）：
- relevant 为空 → 返回 None：该题「没测出」（黄金集没给期望来源），
  不计入均值，绝不冒充 0 分；
- ranked 为空且 relevant 非空 → 返回 0.0：检索空转是**真实失败信号**
  （库空/检索故障就该被分数暴露），与「没测出」性质不同；
- k <= 0 → ValueError（参数错误当场抛，不静默取整）；
- ranked 重复 id：按首次出现位置去重保序（重复项不重复计分、不挤占名额）。

nDCG 折扣：位置 i（0 基）排名 r=i+1，增益 1/log2(r+1)=1/log2(i+2)；
IDCG 按理想序列（前 min(|relevant|,k) 位全命中）归一，二值相关下 ∈ [0,1]。
"""

from __future__ import annotations

import math
from typing import Collection, Sequence


def _dedupe(ranked: Sequence[str]) -> list[str]:
    """重复 id 去重保序：只保留首次出现（后续重复视为同一文档再次返回）。"""
    seen: set[str] = set()
    out: list[str] = []
    for rid in ranked:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def _check_k(k: int) -> None:
    if k <= 0:
        raise ValueError(f"k 必须为正整数，得到 {k!r}")


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float | None:
    """前 k 名命中的期望来源数 / 期望来源总数；relevant 空 → None（没测出≠0）。"""
    _check_k(k)
    if not relevant:
        return None
    rel = set(relevant)
    top = _dedupe(ranked)[:k]
    return len(rel & set(top)) / len(rel)


def reciprocal_rank(ranked: Sequence[str], relevant: Collection[str]) -> float | None:
    """第一个命中期望来源的排名倒数（1/rank）；无命中 → 0.0；relevant 空 → None。

    无 k 截断：窗口由调用方检索的 top_k 决定（run_retrieval_eval 以 max(ks) 取数）。
    """
    if not relevant:
        return None
    rel = set(relevant)
    for i, rid in enumerate(_dedupe(ranked), start=1):
        if rid in rel:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float | None:
    """nDCG@k（二值相关）：DCG=Σ 命中位折扣增益 / 理想序列 DCG；relevant 空 → None。"""
    _check_k(k)
    if not relevant:
        return None
    rel = set(relevant)
    top = _dedupe(ranked)[:k]
    dcg = sum(1.0 / math.log2(i + 2) for i, rid in enumerate(top) if rid in rel)
    ideal = min(len(rel), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal))
    return dcg / idcg  # relevant 非空且 k≥1 时 idcg≥1>0，无除零
