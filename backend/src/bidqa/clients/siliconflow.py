"""SiliconFlow bge-m3 嵌入与重排客户端（docs/01 clients 层；E2/E4）。

约定：查询与文档走同一 API 入口（单测锁定，保证分数可比）；批量、重试、限流退避；
trust_env=False。bge-m3 一次返回稠密 1024 + 学习型稀疏（response.sparse_embedding
缺省时稀疏为 None，由调用方降级）。
"""

from __future__ import annotations

import time

import httpx
from qdrant_client import models


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
    """bge-m3：稠密 1024 + 稀疏一次返回。embed_documents 与 embed_query 调同一 _embed。"""

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
                out.append((dense, sparse))
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
