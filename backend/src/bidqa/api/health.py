"""GET /api/health（docs/01 §3.1；M1-9 实战化）：各源真实探测 + 30s 缓存。

语义：探测失败/超时 → 该源状态 "不可用"（绝不记 0、绝不挂死）；免鉴权；
端点同步 def。探测项：qdrant/pg/neo4j（数据面），embedding/rerank/chat（模型面）。
"""

from __future__ import annotations

import time

import httpx
import psycopg
from fastapi import APIRouter, Depends
from neo4j import GraphDatabase

from bidqa.config import Settings, get_settings

router = APIRouter()

APP_VERSION = "0.1.0"
_CACHE_TTL = 30.0
_cache: dict = {"ts": 0.0, "payload": None}


def _probe(name: str, fn) -> dict:
    t0 = time.perf_counter()
    try:
        fn()
        return {"status": "ok", "latency_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as exc:  # 探测失败 = 不可用（诚实降级）
        return {"status": "不可用", "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": str(exc)[:120]}


def _probe_all(settings: Settings) -> dict:
    results: dict[str, dict] = {}

    def qdrant_probe():
        from bidqa.database.factory import build_repo
        repo = build_repo()
        repo.count()

    results["qdrant"] = _probe("qdrant", qdrant_probe)

    def pg_probe():
        with psycopg.connect(
            host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
            dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD, connect_timeout=3,
        ) as conn:
            conn.execute("SELECT 1")

    results["postgres"] = _probe("postgres", pg_probe)

    def neo4j_probe():
        if not settings.has_neo4j:
            raise RuntimeError("未配置（NEO4J_* 缺失，能力降级）")
        with GraphDatabase.driver(
            settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            connection_timeout=3,
        ) as driver:
            driver.verify_connectivity()

    results["neo4j"] = _probe("neo4j", neo4j_probe)

    def _http_probe(base: str, key: str) -> None:
        with httpx.Client(base_url=base.rstrip("/"), headers={"Authorization": f"Bearer {key}"},
                          timeout=3, trust_env=False) as c:
            if c.get("/models").status_code != 200:
                raise RuntimeError("HTTP 非 200")

    if settings.has_embedding:
        results["embedding"] = _probe("embedding", lambda: _http_probe(
            settings.EMBEDDING_BASE_URL, settings.EMBEDDING_API_KEY))
    if settings.has_rerank:
        results["rerank"] = _probe("rerank", lambda: _http_probe(
            settings.RERANK_BASE_URL, settings.RERANK_API_KEY))
    if settings.auth_enabled or settings.CHAT_API_KEY:
        results["chat"] = _probe("chat", lambda: _http_probe(
            settings.CHAT_BASE_URL, settings.CHAT_API_KEY))
    return results


@router.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    now = time.monotonic()
    if _cache["payload"] is None or now - _cache["ts"] > _CACHE_TTL:
        components = _probe_all(settings)
        _cache["payload"] = components
        _cache["ts"] = now
    components = _cache["payload"]
    return {
        "status": "ok" if all(c["status"] == "ok" for c in components.values()) else "degraded",
        "version": APP_VERSION,
        "auth": "token" if settings.auth_enabled else "dev",
        "capabilities": {
            "chat": settings.has_chat, "qdrant": True,  # qdrant 本地绿装恒配置，可达性看 components 探测
            "thinking": settings.has_thinking, "vision": settings.has_vision,
            "vision_ocr": settings.has_ocr,
            "judge": settings.has_judge, "embedding": settings.has_embedding,
            "rerank": settings.has_rerank, "neo4j": settings.has_neo4j,
            "postgres": settings.has_postgres, "web_search": settings.has_web_search,
            "pdf_api": settings.has_pdf_api,
        },
        "components": components,
        "cache": "30s",
    }
