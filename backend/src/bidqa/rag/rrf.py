"""RRF 融合唯一入口（docs/01 §4.1；E3）。

Qdrant 的 `prefetch + FusionQuery` 只返回融合结果、拿不到各路排名，且
`FusionQuery` 的 k 固定为 2（qdrant_client/hybrid/fusion.py 的
`DEFAULT_RANKING_CONSTANT_K`），E1 分类产出的 rrf_k 无处注入。因此改为
「两路独立 query_points + 本模块按 Σ1/(k+rank) 客户端融合」。
"""

from __future__ import annotations

from qdrant_client import models


def fuse_rrf(branches: list[list[models.ScoredPoint]], k: int,
             limit: int) -> list[models.ScoredPoint]:
    """多路有序结果 → RRF 融合，返回按融合分降序的前 limit 个**新**对象。

    - 每路返回顺序即 rank（1-based），只累加该路中出现的 rank 贡献 1/(k+rank)；
      各路长度不等时互不补齐。
    - 跨路按 `str(point.id)` 去重（`ScoredPoint.id` 是 int|str|UUID 混合），
      保留首现点的 payload；并列时按 `str(id)` 稳定排序，保证可复现。
    - **不就地改输入**：用 `model_copy(deep=True)` 产出新对象再写融合分，
      避免污染另一路与 `_FIFO.put` 存的原对象（缓存返回深拷贝硬约束）。
    - 融合分只表达排名、不表达 0~1 相关度；调用方 `retrieve.py` 不把它写进
      `Hit.score`（取不到记「不可用」，绝不记 0）。
    """
    scores: dict[str, float] = {}
    first: dict[str, models.ScoredPoint] = {}
    for branch in branches:
        for rank, point in enumerate(branch, start=1):
            key = str(point.id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            first.setdefault(key, point)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    out: list[models.ScoredPoint] = []
    for key in ordered[:limit]:
        fused = first[key].model_copy(deep=True)
        fused.score = scores[key]
        out.append(fused)
    return out
