"""pytest 公共夹具：Settings 工厂 + 各级缓存隔离。

测试一律 Settings(_env_file=None) 直构，避免读取真实 backend/.env 污染用例；
service 层 lru_cache 在每个用例前后清空。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from bidqa.config import Settings, get_settings


def _pin_tempdir_outside_repo() -> None:
    """落盘隔离（AGENTS「测试不得污染真实仓库」红线）：任何环境不向仓库写临时文件。

    个别沙箱缺 TEMP/TMP 时 tempfile.gettempdir() 兜底到当前目录（backend/），
    jieba 缓存、pytest 临时根、mkdtemp 产物会全部落进仓库。此处把 tempfile 根
    钉到系统真实临时目录——pytest 的 tmp_path 根与 jieba 缓存同源受益。
    哨兵用例：test_temp_isolation.py。
    """
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    try:
        resolved = os.path.abspath(tempfile.gettempdir())
    except Exception:
        resolved = os.getcwd()
    repo_low = repo.lower()
    if resolved.lower() != repo_low and not resolved.lower().startswith(repo_low + os.sep):
        return
    for cand in (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp"),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp"),
        "/tmp",
    ):
        if cand and os.path.isdir(cand) and os.access(cand, os.W_OK):
            tempfile.tempdir = cand
            return
    raise RuntimeError(
        f"系统临时目录不可用且 tempfile 兜底到仓库内（{resolved}）；"
        "请设置 TEMP/TMP 环境变量后重跑，避免测试落盘污染仓库"
    )


_pin_tempdir_outside_repo()

CORE_ENV = {
    "CHAT_BASE_URL": "https://api.example.com/v1",
    "CHAT_API_KEY": "sk-test",
    "CHAT_MODEL": "test-model",
    "QDRANT_URL": "http://127.0.0.1:6333",
    # 用户体系默认关（旧测试全裸请求，无会话头）；用户体系测试显式传 USERS_ENABLED="true"
    "USERS_ENABLED": "false",
}


@pytest.fixture(autouse=True)
def _reset_caches():
    """每个用例前后清空 get_settings 与各工厂缓存，防跨用例泄漏（C5：工厂归位后按真名清理）。"""
    from bidqa.auth import factory as auth_factory
    from bidqa.clients import chat_factory, web_factory
    from bidqa.database import factory as db_factory
    from bidqa.rag import factory as rag_factory

    def _clear():
        get_settings.cache_clear()
        db_factory.build_repo.cache_clear()
        rag_factory.build_embedding.cache_clear()
        rag_factory.build_reranker.cache_clear()
        rag_factory.build_retriever.cache_clear()
        chat_factory.build_chat.cache_clear()
        chat_factory.build_vision.cache_clear()
        chat_factory.build_ocr.cache_clear()
        chat_factory.clear_profile_chats()
        db_factory.build_pg.cache_clear()
        web_factory.build_web_clients.cache_clear()
        auth_factory.build_users_store.cache_clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def settings_factory():
    """构造核心配置齐全的 Settings；extra 传能力槽位覆盖项。"""

    def _make(**extra: str) -> Settings:
        return Settings(_env_file=None, **{**CORE_ENV, **extra})

    return _make


@pytest.fixture(scope="session")
def pg_client():
    """真实 PG（服务不可用则 skip，CI 兼容）；开发库按当前 schema 重建。"""
    from bidqa.database.connection import PgConn

    s = Settings(_env_file=".env")  # cwd=backend 时读真实 .env 的 POSTGRES_*
    client = PgConn(s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DB,
                      s.POSTGRES_USER, s.POSTGRES_PASSWORD)
    if not client.ping():
        pytest.skip("PostgreSQL 未运行（集成测试跳过，CI 兼容）")
    # 只幂等建表，不 DROP（开发库可能已有真实导入数据；改 schema 时手动重建）
    client.ensure_schema()
    yield client
    client.close()


@pytest.fixture(scope="session")
def neo_client():
    """真实 Neo4j（服务不可用则 skip，CI 兼容）。"""
    from bidqa.database.neo4j import NeoClient

    s = Settings(_env_file=".env")
    client = NeoClient(s.NEO4J_URI, s.NEO4J_USER, s.NEO4J_PASSWORD)
    if not client.ping():
        pytest.skip("Neo4j 未运行（集成测试跳过，CI 兼容）")
    yield client
    client.close()
