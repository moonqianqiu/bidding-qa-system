"""M0 冒烟：配置两级校验（docs/01 §5 / docs/03 M0-2 验收）。"""

import pytest

from bidqa.config import ConfigError, Settings


def test_core_missing_fails_startup():
    s = Settings(_env_file=None)
    assert set(s.missing_core()) == {"CHAT_BASE_URL", "CHAT_API_KEY", "CHAT_MODEL", "QDRANT_URL"}
    with pytest.raises(ConfigError):
        s.validate_core()


def test_core_ok_passes(settings_factory):
    settings_factory().validate_core()  # 不抛即通过


def test_create_app_validates_core(settings_factory, monkeypatch):
    """生产默认路径门禁（审计 P1-6）：`uvicorn bidqa.main:app` 走 create_app()
    无参路径，必须过核心校验——不能绕过 `bidqa serve` 的配置门。显式传 Settings
    的调用方（测试/嵌入式）自带配置责任，不强校验（health 降级用例依赖）。"""
    import bidqa.main as main_mod

    monkeypatch.setattr(main_mod, "get_settings",
                        lambda: settings_factory(CHAT_API_KEY=""))
    with pytest.raises(ConfigError):
        main_mod.create_app()


def test_capability_missing_degrades(settings_factory):
    s = settings_factory()
    assert not s.has_thinking
    assert not s.has_vision
    assert not s.has_judge
    assert not s.has_embedding
    assert not s.has_rerank
    assert not s.has_neo4j  # 密码缺省为空 → 图谱降级
    assert not s.has_web_search
    assert not s.has_pdf_api
    assert not s.auth_enabled  # 开发模式放行


def test_online_eval_defaults(settings_factory):
    """在线评测默认关且采样率 0（决策：行为指标全量免费，点踩 100% 必评）。"""
    s = settings_factory()
    assert s.ONLINE_EVAL_ENABLED is False
    assert s.ONLINE_EVAL_SAMPLE_RATE == 0.0


def test_capability_configured_ready(settings_factory):
    s = settings_factory(
        VISION_BASE_URL="https://v.example.com/v1",
        VISION_API_KEY="k",
        VISION_MODEL="m",
        JUDGE_BASE_URL="https://j.example.com/v1",
        JUDGE_API_KEY="k",
        JUDGE_MODEL="m",
        NEO4J_PASSWORD="pw",
        EMBEDDING_BASE_URL="https://e.example.com/v1",
        EMBEDDING_API_KEY="k",
        EMBEDDING_MODEL="BAAI/bge-m3",
    )
    assert s.has_vision and s.has_judge and s.has_neo4j and s.has_embedding
    assert not s.has_rerank


def test_cors_origins_parsed(settings_factory):
    s = settings_factory(API_CORS_ORIGINS="http://a.dev , http://b.dev,")
    assert s.cors_origins() == ["http://a.dev", "http://b.dev"]


def test_worker_guard_warns_multi_not_rejects(settings_factory, monkeypatch, caplog):
    """P1-29：uvicorn 显式 `workers=1` 时会忽略 WEB_CONCURRENCY——旧防呆据此
    拒绝启动只会误拒运维（该变量根本改变不了 worker 数）。改为告警不拒。"""
    import logging

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with caplog.at_level(logging.WARNING, logger="bidqa.config"):
        settings_factory().check_worker_count()  # 不抛
    assert any("WEB_CONCURRENCY" in r.getMessage() for r in caplog.records)


def test_worker_guard_allows_one(settings_factory, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    with caplog.at_level(logging.WARNING, logger="bidqa.config"):
        settings_factory().check_worker_count()  # 不抛
    assert not [r for r in caplog.records if "WEB_CONCURRENCY" in r.getMessage()]


def test_web_search_providers_roster():
    """WEB_SEARCH_PROVIDERS 名册（D5）：留空=关；未知名/列名缺 key 跳过；保留配置序。"""
    s = Settings(_env_file=None)
    assert s.web_search_providers() == [] and not s.has_web_search  # 留空=联网关闭

    s = Settings(_env_file=None, WEB_SEARCH_PROVIDERS="tavily", TAVILY_API_KEY="tvly-1")
    assert s.web_search_providers() == ["tavily"] and s.has_web_search

    s = Settings(_env_file=None, WEB_SEARCH_PROVIDERS="exa, tavily, bing", TAVILY_API_KEY="k")
    assert s.web_search_providers() == ["tavily"]  # exa 缺 key 跳过；bing 未知名跳过；序保留

    s = Settings(_env_file=None, WEB_SEARCH_PROVIDERS="exa", EXA_API_KEY="exa-1")
    assert s.web_search_providers() == ["exa"] and s.has_web_search  # Exa 单配即真可用


def test_has_chat_slot_or_profile(monkeypatch):
    """capabilities.chat 口径修复：主槽位三件套或任一命名档齐备才算已配置。"""
    assert not Settings(_env_file=None).has_chat
    s = Settings(_env_file=None, CHAT_BASE_URL="https://x/v1", CHAT_API_KEY="k", CHAT_MODEL="m")
    assert s.has_chat  # 主槽位
    monkeypatch.setenv("CHAT_PROFILES", "fast")
    monkeypatch.setenv("CHAT_FAST_BASE_URL", "https://fast/v1")
    monkeypatch.setenv("CHAT_FAST_API_KEY", "k")
    monkeypatch.setenv("CHAT_FAST_MODEL", "m")
    assert Settings(_env_file=None).has_chat  # 命名档齐备
    monkeypatch.setenv("CHAT_FAST_MODEL", "")
    assert not Settings(_env_file=None).has_chat  # 档位缺件跳过 → 回落未配置


def test_users_system_config_defaults():
    """用户体系 5 键（docs/01 §5）：本机默认启用 + 种子管理员默认 + 会话 TTL + 游客限流。"""
    s = Settings(_env_file=None)
    assert s.USERS_ENABLED is True  # 本机默认启用（用户已确认）
    assert s.ADMIN_USERNAME == "admin" and s.ADMIN_PASSWORD == "admin123"
    assert s.SESSION_TTL_HOURS == 72
    assert s.GUEST_RATE_LIMIT == 10
    s2 = Settings(_env_file=None, USERS_ENABLED=False, SESSION_TTL_HOURS=24,
                  GUEST_RATE_LIMIT=5, ADMIN_USERNAME="root", ADMIN_PASSWORD="x")
    assert (s2.USERS_ENABLED, s2.SESSION_TTL_HOURS, s2.GUEST_RATE_LIMIT) == (False, 24, 5)
    assert s2.ADMIN_USERNAME == "root" and s2.ADMIN_PASSWORD == "x"
