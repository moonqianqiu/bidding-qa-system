"""bge-m3 嵌入与重排客户端（docs/01 clients 层；E2/E4）。

约定：查询与文档走同一 API 入口（单测锁定，保证分数可比）；批量、重试、限流退避；
trust_env=False。**端点是否交付学习型稀疏由端点决定**：
- 自建 bge-m3（SLali L20:8002，2026-11-23 起，NIGHT-065）：加载 `sparse_linear.pt` 线性头，
  `embedding` + `sparse_embedding` 一次返回——`sparse` 通道三路全量落库；
- 第三方端点（SiliconFlow）：`/embeddings` 响应缺省无 `sparse_embedding` 键（NIGHT-040
  旁挂取证 20/20 仅 embedding/index/object）——客户端按 None 降级、ingest 跳过 `sparse` 通道，
  hybrid 退化为 dense 单路；本批占比为 0 时打一次 WARNING（NIGHT-045 可观测性，不拒收、不改返回形态）。
换端点只改 `EMBEDDING_*` 配置，不改代码。
"""

from __future__ import annotations

import logging
import time

import httpx
from qdrant_client import models

logger = logging.getLogger(__name__)


class DataSourceError(RuntimeError):
    """外部数据源不可用（对应 5031）。"""

    def __init__(self, source: str, message: str):
        super().__init__(f"[{source}] {message}")
        self.source = source


def _post_with_retry(client: httpx.Client, url: str, payload: dict,
                     source: str, max_attempts: int = 3, retry_429: bool = False) -> dict:
    """POST JSON；5xx/网络错误退避重试（1s/2s/4s）；**429 缺省不重试**（立即报错）。

    429 只对 token.sensenova.cn 定向重试（AGENTS 红线 / docs/01 §5）：由 ChatClient
    按端点白名单显式传 retry_429=retry_429_eligible(base_url)；嵌入/重排/Tavily/Exa
    等其余端点一律缺省 False——审计 P0-3（2026-11-15）：原默认 True 曾全局重试 429。
    """
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and not retry_429:
                raise DataSourceError(source, f"HTTP 429: {resp.text[:200]}")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise DataSourceError(source, f"HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.HTTPError as exc:  # 连接/超时
            last = exc
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise DataSourceError(source, f"网络错误: {exc}") from exc
    raise DataSourceError(source, f"重试耗尽: {last}")


class EmbeddingClient:
    """bge-m3：稠密 1024 必返；学习型稀疏当响应含 sparse_embedding 时取用（自建端点恒返回；
    第三方端点可能缺该键 → None 降级）。embed_documents 与 embed_query 调同一 _embed。"""

    # NIGHT-045：单入口（_embed）进程级只打一次 sparse 空置 WARNING——查询/逐批不刷屏
    _sparse_zero_warned: bool = False

    def __init__(self, base_url: str, api_key: str, model: str = "BAAI/bge-m3",
                 batch_size: int = 16, timeout: float = 30.0):
        self.model = model
        self.batch_size = batch_size
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            trust_env=False,
        )

    def embed_documents(self, texts: list[str]) -> list[tuple[list[float], models.SparseVector | None]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> tuple[list[float], models.SparseVector | None]:
        return self._embed([text])[0]

    def _embed(self, texts: list[str]) -> list[tuple[list[float], models.SparseVector | None]]:
        out: list[tuple[list[float], models.SparseVector | None]] = []
        total = sparse_items = 0  # NIGHT-045：本批 sparse item 占比观测（单入口收口）
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            data = _post_with_retry(
                self._client, "/embeddings",
                {"model": self.model, "input": batch, "encoding_format": "float"},
                source="embedding",
            )
            for item in data["data"]:
                dense = item["embedding"]
                sparse = None
                sp = item.get("sparse_embedding")
                if sp and sp.get("indices") is not None:
                    sparse = models.SparseVector(indices=sp["indices"], values=sp["values"])
                    sparse_items += 1
                total += 1
                out.append((dense, sparse))
        if total and sparse_items == 0 and not EmbeddingClient._sparse_zero_warned:
            EmbeddingClient._sparse_zero_warned = True
            logger.warning(
                "sparse 观测：本批 0/%d 条 item 返回 sparse_embedding（占比 0%%）"
                "——学习型 sparse 通道不写入，hybrid 退化为 dense 单路（换端点/换集合后出现即核查配置）",
                total)
        return out

    def ping(self) -> bool:
        try:
            resp = self._client.get("/models")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()


class RerankClient:
    """bge-reranker-v2-m3：POST /rerank → results[{index, relevance_score}]。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 30.0):
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            trust_env=False,
        )

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """返回 [(原索引, 相关度分)]，按分降序，截断 top_n。"""
        if not documents:
            return []
        data = _post_with_retry(
            self._client, "/rerank",
            {"model": self.model, "query": query, "documents": documents,
             "top_n": top_n, "return_documents": False},
            source="rerank",
        )
        return [(r["index"], float(r["relevance_score"])) for r in data["results"]]

    def ping(self) -> bool:
        try:
            resp = self._client.get("/models")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()
