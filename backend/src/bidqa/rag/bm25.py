"""jieba BM25 灾备通道（docs/01 §4.1 sparse_bm25）。

词元→索引 用 crc32 稳定映射（无需持久化词表，入库与查询两侧天然一致）；
值 = 词频（Qdrant 稀疏向量按权重匹配）。嵌入 API 不可用/断网时，查询与召回
全走本通道，不依赖任何外部 API。
"""

from __future__ import annotations

import zlib
from zlib import crc32

import jieba
from qdrant_client import models

_MAX_INDEX = 2 ** 31 - 1  # Qdrant 稀疏索引为 u32，取安全上限


def tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text) if t.strip()]


def sparse_bm25(text: str) -> models.SparseVector:
    """文本 → 稳定索引稀疏向量（indices=crc32(token), values=词频）。"""
    tf: dict[int, int] = {}
    for token in tokenize(text):
        idx = crc32(token.encode("utf-8")) % _MAX_INDEX
        tf[idx] = tf.get(idx, 0) + 1
    indices = sorted(tf)
    return models.SparseVector(indices=indices, values=[float(tf[i]) for i in indices])
