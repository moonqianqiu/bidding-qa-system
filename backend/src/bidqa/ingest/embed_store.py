"""F4：嵌入入库（docs/02 F4）——payload 校验拒收 + 向量写入 + 幂等批次。

写入通道：schema 声明三路，实际 dense+sparse_bm25 必写；学习型 sparse 仅在
提供商返回 sparse_embedding 时写（当前端点实测不返回，docs/01 §4.1 口径债）。
灾备语义：嵌入 API 不可用 → 仅 sparse_bm25
（拒绝整批写入，返回降级说明——保证集合内向量形态一致）。

编码口径（审计 embed_store 债）：三条向量通道共用 `encode_text(chunk)`
（title + 可选 ctx_summary + text）——此前 dense/sparse 看 `ctx_summary+text`、
sparse_bm25 看 `title+text`，同一 chunk 在不同通道检索语义漂移。既有集合需重新
ingest 才与新口径一致（docs/01 §4.1）。

sparse_bm25 文档侧烘焙（NIGHT-042 方案 B1，docs/01 §4.1 参数表）：值 =
BM25 文档权重（tf 饱和 k1=1.5 + 长度归一 b=0.75），查询侧只发原始词频
（rag/bm25.sparse_query），两侧点积 ≈ 无 IDF 的 BM25 分数。

avgdl 口径与漂移治理：norm 依赖**全集合**平均词元数。全量 ingest=本批均值
（即集合均值，精确）；增量 ingest 用本批均值近似、会使 avgdl 漂移——增量后跑
一次通道重算归位（docs/部署手册 §3 数据面命令，scroll 全集合 payload 重分词
重烘焙，纯本地 CPU、零 API 调用）。
"""

from __future__ import annotations

from dataclasses import dataclass

from bidqa.clients.siliconflow import DataSourceError, EmbeddingClient
from bidqa.database.qdrant import Chunk, QdrantRepo, validate_chunk
from bidqa.rag.bm25 import sparse_bm25_doc, tokenize


@dataclass
class IngestReport:
    total: int
    rejected: list[str]
    embedded: bool      # dense 是否写入（嵌入 API 可用且有有效块）
    upserted: int
    sparse_written: int = 0  # 学习型 sparse 实际写入条数（0=提供商未返回，通道跳过；NIGHT-045）


def encode_text(chunk: Chunk) -> str:
    """chunk → 统一编码串（dense/sparse/bm25 同源；空段不产生空行）。"""
    parts = [chunk.title.strip(), chunk.ctx_summary.strip(), chunk.text]
    return "\n".join(p for p in parts if p)


def ingest_chunks(repo: QdrantRepo, chunks: list[Chunk],
                  embedding: EmbeddingClient | None, batch_size: int = 64) -> IngestReport:
    total = len(chunks)
    rejected: list[str] = []
    valid: list[Chunk] = []
    for c in chunks:
        err = validate_chunk(c)
        if err:
            rejected.append(f"{c.title[:30]}: {err}")
        else:
            valid.append(c)

    encode = [encode_text(c) for c in valid]
    # avgdl：本批平均词元数（全量 ingest=集合均值；增量批次=近似，漂移后跑通道
    # 重算归位——docs/部署手册 §3；见模块 docstring「avgdl 口径与漂移治理」）。
    dl_list = [len(tokenize(s)) for s in encode]
    avgdl = (sum(dl_list) / len(dl_list)) if dl_list else 0.0
    use_dense = embedding is not None and bool(valid)
    dense: list[list[float] | None] = [None] * len(valid)
    sparse: list[object | None] = [None] * len(valid)
    if use_dense:
        try:
            embedded = embedding.embed_documents(encode)
        except DataSourceError as exc:
            raise RuntimeError(f"嵌入 API 不可用且灾备策略要求整批拒绝：{exc}") from exc
        dense = [e[0] for e in embedded]
        sparse = [e[1] for e in embedded]

    upserted = 0
    for i in range(0, len(valid), batch_size):
        part = valid[i:i + batch_size]
        bm25 = [sparse_bm25_doc(text, avgdl) for text in encode[i:i + batch_size]]
        upserted += repo.upsert(
            part, dense[i:i + batch_size], sparse[i:i + batch_size], bm25,
        )
    # 学习型 sparse 实写条数（upsert 对 None 跳通道，database/qdrant.py——与落库一致）
    sparse_written = sum(1 for s in sparse if s is not None) if use_dense else 0
    return IngestReport(total=total, rejected=rejected, embedded=use_dense,
                        upserted=upserted, sparse_written=sparse_written)
