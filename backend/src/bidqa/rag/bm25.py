"""jieba BM25 灾备通道（docs/01 §4.1 sparse_bm25）。

词元→索引 用 crc32 稳定映射（无需持久化词表，入库与查询两侧天然一致）。
两侧语义不对称（NIGHT-042 方案 B1，docs/01 §4.1 参数表）：
- 查询侧 `sparse_query`：值 = 原始词频 qtf；
- 文档侧 `sparse_bm25_doc`：值 = 烘焙 BM25 文档权重 tf·(k1+1)/(tf+k1·norm)——
  tf 饱和（k1）+ 长度归一（b，norm=1-b+b·dl/avgdl），无 IDF（不依赖 df 工件；
  NIGHT-037 实验 B1：R@5/R@10=1.0、MRR=0.896，参数网格平稳无需精调）。
两侧点积 ≈ 无 IDF 的 BM25 分数。嵌入 API 不可用/断网时，查询与召回全走本通道，
不依赖任何外部 API。

avgdl 口径：全集合平均词元数（dl 与 tokenize 同口径）。全量 ingest 时=本批均值
（即集合均值）；增量 ingest 会使 avgdl 漂移——跑一次通道重算归位（部署手册
§3 数据面命令，纯本地 CPU、零 API 调用），见 embed_store.ingest_chunks docstring。
"""

from __future__ import annotations

from zlib import crc32

import jieba
from qdrant_client import models

_MAX_INDEX = 2 ** 31 - 1  # Qdrant 稀疏索引为 u32，取安全上限

DEFAULT_K1 = 1.5  # tf 饱和强度（NIGHT-037 §4.3 敏感性网格：R@10 恒 1.0）
DEFAULT_B = 0.75  # 长度归一强度（同上，标准值）


def tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text) if t.strip()]


def _term_frequencies(tokens: list[str]) -> dict[int, int]:
    tf: dict[int, int] = {}
    for token in tokens:
        idx = crc32(token.encode("utf-8")) % _MAX_INDEX
        tf[idx] = tf.get(idx, 0) + 1
    return tf


def sparse_query(text: str) -> models.SparseVector:
    """查询侧：文本 → 稳定索引稀疏向量（indices=crc32(token), values=原始词频 qtf）。"""
    tf = _term_frequencies(tokenize(text))
    indices = sorted(tf)
    return models.SparseVector(indices=indices, values=[float(tf[i]) for i in indices])


def doc_weight(tf: float, dl: float, avgdl: float,
               k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> float:
    """单词元的 BM25 文档权重（docs/01 §4.1 参数表单一来源）。

    tf·(k1+1)/(tf+k1·norm)，norm=1-b+b·dl/avgdl。锚点性质：dl=avgdl 且 tf=1
    → 权重恰 1.0（与旧词频口径对齐）；tf 越大饱和、dl 越偏离 avgdl 越被压/抬。
    avgdl<=0 视为 norm=1（空集合退化，不除零）。
    """
    norm = 1.0 if avgdl <= 0 else 1.0 - b + b * dl / avgdl
    return tf * (k1 + 1.0) / (tf + k1 * norm)


def sparse_bm25_doc(text: str, avgdl: float,
                    k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> models.SparseVector:
    """文档侧：文本 → 烘焙 BM25 权重的稳定索引稀疏向量（ingest 入库用）。

    dl = 词元总数（与 tokenize 同口径）；同输入恒等输出（幂等——通道重算脚本
    可重复执行，重算口径见 docs/部署手册 §3）。
    """
    tokens = tokenize(text)
    tf = _term_frequencies(tokens)
    dl = float(len(tokens))
    indices = sorted(tf)
    values = [doc_weight(float(tf[i]), dl, avgdl, k1, b) for i in indices]
    return models.SparseVector(indices=indices, values=values)
