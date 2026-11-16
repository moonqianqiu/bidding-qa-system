"""F4：嵌入入库（docs/02 F4）——payload 校验拒收 + 三路向量写入 + 幂等批次。

灾备语义：嵌入 API 可用 → dense+sparse+sparse_bm25 三路；不可用 → 仅 sparse_bm25
（拒绝整批写入，返回降级说明——保证集合内向量形态一致）。

编码口径（审计 embed_store 债）：三条向量通道共用 `encode_text(chunk)`
（title + 可选 ctx_summary + text）——此前 dense/sparse 看 `ctx_summary+text`、
sparse_bm25 看 `title+text`，同一 chunk 在不同通道检索语义漂移。既有集合需重新
ingest 才与新口径一致（docs/01 §4.1）。
"""

from __future__ import annotations

from dataclasses import dataclass

from bidqa.clients.siliconflow import DataSourceError, EmbeddingClient
from bidqa.database.qdrant import Chunk, QdrantRepo, validate_chunk
from bidqa.rag.bm25 import sparse_bm25


@dataclass
class IngestReport:
    total: int
    rejected: list[str]
    embedded: bool      # dense+sparse 是否写入
    upserted: int


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
        bm25 = [sparse_bm25(text) for text in encode[i:i + batch_size]]
        upserted += repo.upsert(
            part, dense[i:i + batch_size], sparse[i:i + batch_size], bm25,
        )
    return IngestReport(total=total, rejected=rejected, embedded=use_dense, upserted=upserted)
