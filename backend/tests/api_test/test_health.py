"""M1-9：健康检查（真实探测外壳 + 30s 缓存 + 降级语义）。"""

import pytest
from fastapi.testclient import TestClient

from bidqa.api import health as health_mod
from bidqa.main import create_app

CANNED = {
    "qdrant": {"status": "ok", "latency_ms": 1},
    "postgres": {"status": "不可用", "latency_ms": 2, "error": "connect refused"},
}


@pytest.fixture(autouse=True)
def _reset_health_cache():
    """health 模块级缓存按用例复位。"""
    health_mod._cache = {"ts": 0.0, "payload": None}
    yield
    health_mod._cache = {"ts": 0.0, "payload": None}


def test_health_shape_and_degradation(settings_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(health_mod, "_probe_all", lambda s: calls.append(1) or dict(CANNED))
    client = TestClient(create_app(settings_factory()))
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"  # postgres 不可用 → 如实降级
    assert body["components"]["qdrant"]["status"] == "ok"
    assert body["components"]["postgres"]["status"] == "不可用"
    assert "error" in body["components"]["postgres"]
    assert body["cache"] == "30s"


def test_health_uses_30s_cache(settings_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(health_mod, "_probe_all", lambda s: calls.append(1) or dict(CANNED))
    client = TestClient(create_app(settings_factory()))
    client.get("/api/health")
    client.get("/api/health")
    client.get("/api/health")
    assert len(calls) == 1  # 30s 内只探测一次


def test_health_all_ok_when_everything_up(settings_factory, monkeypatch):
    ok = {"qdrant": {"status": "ok", "latency_ms": 1}, "postgres": {"status": "ok", "latency_ms": 1},
          "neo4j": {"status": "ok", "latency_ms": 1}}
    monkeypatch.setattr(health_mod, "_probe_all", lambda s: ok)
    client = TestClient(create_app(settings_factory(NEO4J_PASSWORD="pw")))
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["capabilities"]["neo4j"] is True


def test_health_capabilities_chat_matches_configuration(settings_factory, monkeypatch):
    """capabilities.chat 口径 = 已配置（has_chat），不再恒 True。"""
    monkeypatch.setattr(health_mod, "_probe_all", lambda s: dict(CANNED))
    client = TestClient(create_app(settings_factory()))
    assert client.get("/api/health").json()["capabilities"]["chat"] is True  # CORE_ENV 三件套齐

    from bidqa.config import Settings
    # 显式清空三件套：pydantic-settings 仍会读环境变量，CI 注入 dummy 时 _env_file=None 不足以表达「什么都没配」
    blank = Settings(_env_file=None, CHAT_BASE_URL="", CHAT_API_KEY="", CHAT_MODEL="")
    client2 = TestClient(create_app(blank))
    assert client2.get("/api/health").json()["capabilities"]["chat"] is False  # 什么都没配


def test_health_capabilities_vision_ocr(settings_factory, monkeypatch):
    """capabilities.vision_ocr 口径 = has_ocr（OCR_* 三件套齐备，v2.3 双通道）。"""
    monkeypatch.setattr(health_mod, "_probe_all", lambda s: dict(CANNED))
    client = TestClient(create_app(settings_factory()))
    assert client.get("/api/health").json()["capabilities"]["vision_ocr"] is False

    client2 = TestClient(create_app(settings_factory(
        OCR_BASE_URL="http://ocr.local/v1", OCR_API_KEY="k", OCR_MODEL="ocr-vl")))
    assert client2.get("/api/health").json()["capabilities"]["vision_ocr"] is True
