"""B1 conversations/feedback 端点（4041 语义 + 真实 PG 集成 + 降级 5031）。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from bidqa.main import create_app


@pytest.fixture()
def client(settings_factory, monkeypatch):
    app = create_app(settings_factory())
    return TestClient(app)


def test_conversation_crud_roundtrip(client, pg_client) -> None:  # noqa: ARG001
    conv_id = str(uuid.uuid4())
    try:
        pg_client.upsert_conversation(conv_id, "空调问答",
                                      {"schema_v": 1, "items": [{"role": "user", "content": "q"}]})
        resp = client.get("/api/conversations")
        assert resp.status_code == 200
        assert any(c["id"] == conv_id for c in resp.json()["conversations"])

        got = client.get(f"/api/conversations/{conv_id}")
        assert got.status_code == 200 and got.json()["title"] == "空调问答"

        deleted = client.delete(f"/api/conversations/{conv_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/conversations/{conv_id}").status_code == 404
    finally:
        # 断言中途失败也不泄漏（2026-11-08 教训：失败期的 18 行「新标题/空调问答」就是这么积的）
        pg_client.delete_conversation(conv_id)


def test_conversation_4041_semantics(client, pg_client) -> None:  # noqa: ARG001
    """非法标识与不存在一律 404/4041（docs/01 §3.1）。"""
    assert client.get("/api/conversations/not-a-uuid").status_code == 404
    assert client.get("/api/conversations/not-a-uuid").json()["error_code"] == 4041
    assert client.delete(f"/api/conversations/{uuid.uuid4()}").status_code == 404


def test_conversation_rename_patch(client, pg_client) -> None:  # noqa: ARG001
    """PATCH 重命名：改名生效 / 标题校验 / 非法 id 与不存在 404（WP4）。"""
    conv_id = str(uuid.uuid4())
    try:
        pg_client.upsert_conversation(conv_id, "旧标题",
                                      {"schema_v": 1, "items": [{"role": "user", "content": "q"}]})
        ok = client.patch(f"/api/conversations/{conv_id}", json={"title": "  新标题  "})
        assert ok.status_code == 200 and ok.json() == {"id": conv_id, "title": "新标题"}
        assert client.get(f"/api/conversations/{conv_id}").json()["title"] == "新标题"  # 已 strip

        assert client.patch(f"/api/conversations/{conv_id}", json={"title": ""}).status_code == 400
        assert client.patch("/api/conversations/not-a-uuid",
                            json={"title": "x"}).status_code == 404
        assert client.patch(f"/api/conversations/{uuid.uuid4()}",
                            json={"title": "x"}).status_code == 404
    finally:
        pg_client.delete_conversation(conv_id)  # 失败也不泄漏（同 crud_roundtrip 教训）


def test_feedback_roundtrip(client, pg_client) -> None:  # noqa: ARG001
    resp = client.post("/api/feedback", json={
        "question": "空调采购有哪些？", "rating": "down",
        "sources": [{"id": "chunk-1", "title": "法规"}],
        "trace_id": "api-fb-1"})
    assert resp.status_code == 200 and resp.json()["rating"] == "down"

    bad = client.post("/api/feedback", json={"question": "q", "rating": "middle"})
    assert bad.status_code == 400  # pydantic pattern 校验

    bad_conv = client.post("/api/feedback", json={
        "question": "q", "rating": "up", "conversation_id": "xxx"})
    assert bad_conv.status_code == 404


def test_conversations_degraded_5031(client, settings_factory, monkeypatch) -> None:
    """PG 未配置 → 能力降级如实 5031（不假装成功）。"""
    import bidqa.api.conversations as conv_mod
    monkeypatch.setattr(conv_mod, "build_pg", lambda: None)
    assert client.get("/api/conversations").status_code == 503
    assert client.get("/api/conversations").json()["error_code"] == 5031


# ── 用户体系（docs/01 §3.1 会话隔离，端点级；store 替身 + 真实开发库，IT-auth 清理）──
def _armed(monkeypatch, settings_factory, ident):
    """armed 应用 + 固定身份（store 替身；PG 走 build_pg → 真实开发库）。"""
    from types import SimpleNamespace

    import bidqa.api.middleware as mw
    store = SimpleNamespace(resolve=lambda t: ident if t == "t-fixed" else None,
                            guest_enabled=lambda: True)
    monkeypatch.setattr(mw, "build_users_store", lambda: store)
    return TestClient(create_app(settings_factory(USERS_ENABLED="true")))


def test_scoping_isolation_through_endpoint(pg_client, settings_factory, monkeypatch) -> None:
    """普通用户 A/B 互不可见：list 过滤 + 越权 get/delete/rename 一律 404（不泄漏存在性）。"""
    from bidqa.auth_core import Identity

    uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    ident = Identity("user", uid_a, "alice", "t-fixed")
    client2 = _armed(monkeypatch, settings_factory, ident)
    ids = {k: str(uuid.uuid4()) for k in ("a", "b", "m")}
    try:
        pg_client.upsert_conversation(ids["a"], "A的会话",
                                      {"schema_v": 1, "items": []}, user_id=uid_a)
        pg_client.upsert_conversation(ids["b"], "B的会话",
                                      {"schema_v": 1, "items": []}, user_id=uid_b)
        pg_client.upsert_conversation(ids["m"], "机器域会话", {"schema_v": 1, "items": []})

        auth = {"Authorization": "Bearer t-fixed"}
        listed = client2.get("/api/conversations", headers=auth)
        assert listed.status_code == 200, listed.text
        got_ids = {c["id"] for c in listed.json()["conversations"]}
        assert ids["a"] in got_ids
        assert ids["b"] not in got_ids and ids["m"] not in got_ids  # 他人与机器域不可见

        assert client2.get(f"/api/conversations/{ids['a']}", headers=auth).status_code == 200
        assert client2.get(f"/api/conversations/{ids['b']}", headers=auth).status_code == 404  # 越权=404
        assert client2.delete(f"/api/conversations/{ids['b']}", headers=auth).status_code == 404
        assert client2.patch(f"/api/conversations/{ids['b']}", headers=auth,
                             json={"title": "夺权"}).status_code == 404
        assert pg_client.get_conversation(ids["b"]) is not None  # 越权删除确实未生效
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM conversations WHERE id::text = ANY(%s)",
                     (list(ids.values()),))
        pg_client.commit()


def test_scoping_admin_sees_machine_domain(pg_client, settings_factory, monkeypatch) -> None:
    """人类 admin 可见本人 + 机器域（NULL）；机器主 token 仅 NULL（docs/01 §3.1）。"""
    from bidqa.auth_core import Identity
    from types import SimpleNamespace

    import bidqa.api.middleware as mw

    uid_admin = str(uuid.uuid4())
    machine_id, own_id = str(uuid.uuid4()), str(uuid.uuid4())
    pg_client.upsert_conversation(machine_id, "机器域会话", {"schema_v": 1, "items": []})
    pg_client.upsert_conversation(own_id, "管理员的", {"schema_v": 1, "items": []},
                                  user_id=uid_admin)
    try:
        def make_store(user_id):
            ident = Identity("admin", user_id,
                             "__machine__" if user_id is None else "root", "t-fixed")
            return SimpleNamespace(resolve=lambda t: ident if t == "t-fixed" else None,
                                   guest_enabled=lambda: True)

        monkeypatch.setattr(mw, "build_users_store", lambda: make_store(None))
        machine_client = TestClient(create_app(settings_factory(USERS_ENABLED="true")))
        got = {c["id"] for c in machine_client.get(
            "/api/conversations", headers={"Authorization": "Bearer t-fixed"}).json()["conversations"]}
        assert machine_id in got and own_id not in got  # 机器主 token 仅 NULL 域

        monkeypatch.setattr(mw, "build_users_store", lambda: make_store(uid_admin))
        admin_client = TestClient(create_app(settings_factory(USERS_ENABLED="true")))
        got2 = {c["id"] for c in admin_client.get(
            "/api/conversations", headers={"Authorization": "Bearer t-fixed"}).json()["conversations"]}
        assert {machine_id, own_id} <= got2  # admin 含机器域
    finally:
        conn = pg_client.connection()
        conn.execute("DELETE FROM conversations WHERE id::text = ANY(%s)",
                     ([machine_id, own_id],))
        pg_client.commit()
