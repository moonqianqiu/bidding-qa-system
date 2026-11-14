"""控制台端点契约（docs/02 A15/B11/B12）：三库聚合 / 用户管理 / 游客开关 / 全库会话。

armed 模式内联 FakeUsersStore（tests/ 非 package，不赌跨文件 import——拷贝
test_auth.py 的 armed helper）；全库会话 SQL 的真实 PG 集成在 db_test/test_postgres_integration.py。
聚合缓存是模块级（30s），每个用例前清空防跨用例串味。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import bidqa.api.system as sys_mod
from bidqa.auth_core import Identity
from bidqa.main import create_app

TOKEN = "secret-token"


class FakeUsersStore:
    """armed 替身：中间件/auth 端点所需 + 控制台管理动作（记录调用供断言）。"""

    def __init__(self, sessions: dict[str, Identity] | None = None, guest_on: bool = True):
        self.sessions = dict(sessions or {})
        self.guest_on = guest_on
        self.users: list[dict] = [{"username": "admin1", "role": "admin", "disabled": False,
                                   "created_at": "2026-11-08", "sessions": 1}]
        self.created: list[tuple[str, str]] = []
        self.reset_to: dict[str, int] = {}  # username → 吊销会话数
        self.disabled: list[tuple[str, bool]] = []
        self.guest_toggles: list[bool] = []

    def resolve(self, token: str) -> Identity | None:
        return self.sessions.get(token)

    def guest_enabled(self) -> bool:
        return self.guest_on

    def login(self, username: str, password: str, ttl_hours: int):
        return None  # 控制台用例不需要真登录

    def login_guest(self, ttl_hours: int):
        if not self.guest_on:
            return None
        return {"token": "t-guest", "username": "guest", "role": "guest"}

    def logout(self, token: str) -> bool:
        return token in self.sessions

    # ── 管理动作 ──
    def list_users(self) -> list[dict]:
        return [dict(u) for u in self.users]

    def count_users(self) -> int:
        return len(self.users)

    def create_user(self, username: str, password: str, role: str) -> str | None:
        if any(u["username"] == username for u in self.users):
            return None
        self.created.append((username, role))
        self.users.append({"username": username, "role": role, "disabled": False,
                           "created_at": "-", "sessions": 0})
        return str(uuid.uuid4())

    def reset_password(self, username: str, password: str) -> int | None:
        if not any(u["username"] == username for u in self.users):
            return None
        return self.reset_to.get(username, 0)

    def set_disabled(self, username: str, disabled: bool) -> bool:
        if not any(u["username"] == username for u in self.users):
            return False
        self.disabled.append((username, disabled))
        for u in self.users:
            if u["username"] == username:
                u["disabled"] = disabled
        return True

    def set_guest_enabled(self, enabled: bool) -> None:
        self.guest_toggles.append(enabled)
        self.guest_on = enabled


class FakeConn:
    """console 聚合用假连接：execute(sql) 按 SQL 里的表名回计数。"""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def execute(self, sql: str, *args):
        row = {"n": 0}
        for table, n in self.counts.items():
            if table in sql:
                row["n"] = n
        return _Row(row)


class _Row:
    def __init__(self, row: dict):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class FakePg:
    """全库会话端点替身：列表/删除/盘点/清空全部记录调用。"""

    def __init__(self):
        self.conversations = [
            {"id": str(uuid.uuid4()), "title": "机器域会话", "provider": "p",
             "updated_at": "2026-11-08T10:00:00Z", "user_id": None, "owner": None,
             "items": [{"role": "assistant", "content": "a"}], "has_reply": True},
            {"id": str(uuid.uuid4()), "title": "bob 的会话", "provider": "p",
             "updated_at": "2026-11-08T11:00:00Z", "user_id": "u-bob", "owner": "bob",
             "items": [], "has_reply": False},
        ]
        self.deleted: list[str] = []
        self.purged = False
        self.empty_stats = {"count": 2, "samples": ["新标题", "空调问答"]}

    def connection(self):
        return FakeConn({"bidding_procurement": 8489, "conversations": 2, "feedback": 0})

    def list_all_conversations(self, limit: int = 200, q: str = "") -> list[dict]:
        rows = self.conversations
        if q:
            rows = [r for r in rows if q.lower() in r["title"].lower()]
        return rows[:limit]

    def delete_conversation(self, conv_id: str, *args, **kwargs) -> bool:
        if not any(c["id"] == conv_id for c in self.conversations):
            return False
        self.deleted.append(conv_id)
        self.conversations = [c for c in self.conversations if c["id"] != conv_id]
        return True

    def empty_conversations(self, protect_minutes: int = 10) -> dict:
        return dict(self.empty_stats)

    def purge_empty_conversations(self, protect_minutes: int = 10) -> int:
        self.purged = True
        return 2


class FakeRepo:
    def count(self) -> int:
        return 123


class FakeNeo:
    def counts(self) -> dict[str, int]:
        return {"Project": 8489, "SubjectMatter": 2188, "Supplier": 4001,
                "Purchaser": 3504, "Agency": 306, "Location": 40}


def patch_admin(monkeypatch, store: FakeUsersStore, pg: FakePg | None = None) -> None:
    """system 模块命名空间整体替换（import 点收口在 sys_mod）。"""
    import bidqa.api.auth as auth_mod
    import bidqa.api.middleware as mw

    monkeypatch.setattr(mw, "build_users_store", lambda: store)
    monkeypatch.setattr(auth_mod, "build_users_store", lambda: store)
    monkeypatch.setattr(sys_mod, "build_users_store", lambda: store)
    monkeypatch.setattr(sys_mod, "build_pg", lambda: pg)
    monkeypatch.setattr(sys_mod, "build_repo", lambda: FakeRepo())
    monkeypatch.setattr(sys_mod, "build_neo", lambda: FakeNeo())


def admin_client(monkeypatch, settings_factory, store: FakeUsersStore,
                 pg: FakePg | None = None) -> TestClient:
    ident = Identity("admin", str(uuid.uuid4()), "admin1", "t1")
    store.sessions["t1"] = ident  # 种入管理员会话（auth("t1") 头可解析）
    patch_admin(monkeypatch, store, pg)
    monkeypatch.setattr(sys_mod, "_console_cache", None, raising=False)
    settings = settings_factory(USERS_ENABLED="true")
    return TestClient(create_app(settings))


def auth(token: str = "t1") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _console_cache_reset():
    sys_mod._console_cache = None
    yield
    sys_mod._console_cache = None


# ── 聚合端点 ──
def test_console_aggregate_shape(settings_factory, monkeypatch):
    store = FakeUsersStore()
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    r = client.get("/api/system/console", headers=auth())
    assert r.status_code == 200
    data = r.json()
    assert data["pg"] == {"bidding_procurement": 8489, "conversations": 2,
                          "feedback": 0, "users": 1}
    assert data["qdrant"] == {"vectors": 123, "collection": "bidqa_knowledge_v1"}
    assert data["neo4j"]["total"] == 18528  # 8489+2188+4001+3504+306+40
    assert set(data["neo4j"]["labels"]) == {"Project", "SubjectMatter", "Supplier",
                                            "Purchaser", "Agency", "Location"}


def test_console_aggregate_degraded_db_marks_unavailable(settings_factory, monkeypatch):
    """qdrant 挂 → 该节「不可用」其余节照常（绝不记 0）。"""

    def boom():
        raise RuntimeError("qdrant down")

    store = FakeUsersStore()
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    monkeypatch.setattr(sys_mod, "build_repo", boom)
    monkeypatch.setattr(sys_mod, "_console_cache", None, raising=False)
    r = client.get("/api/system/console", headers=auth("t1"))
    assert r.status_code == 200
    data = r.json()
    assert data["qdrant"] == {"status": "不可用"}
    assert data["pg"]["bidding_procurement"] == 8489  # 其余节不受牵连
    assert data["neo4j"]["total"] == 18528


def test_console_cache_within_ttl(settings_factory, monkeypatch):
    """30s 缓存：第二次命中缓存，底层数据变了也不重算（health 同款语义）。"""
    store = FakeUsersStore()
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    first = client.get("/api/system/console", headers=auth()).json()
    assert sys_mod._console_cache is not None
    store.users.append({"username": "bob", "role": "user", "disabled": False,
                        "created_at": "-", "sessions": 0})
    second = client.get("/api/system/console", headers=auth()).json()
    assert second["pg"]["users"] == first["pg"]["users"] == 1  # 缓存期内不重算


def test_console_forbidden_for_non_admin(settings_factory, monkeypatch):
    ident = Identity("user", "u-1", "alice", "t1")
    store = FakeUsersStore({"t1": ident})
    patch_admin(monkeypatch, store, FakePg())
    client = TestClient(create_app(settings_factory(USERS_ENABLED="true")))
    for path in ("/api/system/console", "/api/system/users",
                 "/api/system/conversations"):
        r = client.get(path, headers=auth("t1"))
        assert r.status_code == 403
        assert r.json()["error_code"] == 4031
    # providers 不在前缀内：普通用户档位下拉可达（回归）
    assert client.get("/api/system/providers", headers=auth("t1")).status_code == 200


# ── 用户管理 ──
def test_users_list_includes_sessions(settings_factory, monkeypatch):
    client = admin_client(monkeypatch, settings_factory, FakeUsersStore(), FakePg())
    r = client.get("/api/system/users", headers=auth())
    assert r.status_code == 200
    users = r.json()["users"]
    assert users[0]["username"] == "admin1"
    assert users[0]["sessions"] == 1  # LEFT JOIN 活跃会话数


def test_user_create_and_duplicate_4091(settings_factory, monkeypatch):
    store = FakeUsersStore()
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    ok = client.post("/api/system/users", headers=auth(),
                     json={"username": "  bob ", "password": "pw-bob", "role": "user"})
    assert ok.status_code == 200 and ok.json()["username"] == "bob"  # strip 生效
    assert store.created == [("bob", "user")]
    dup = client.post("/api/system/users", headers=auth(),
                      json={"username": "bob", "password": "pw-bob", "role": "user"})
    assert dup.status_code == 409 and dup.json()["error_code"] == 4091
    short = client.post("/api/system/users", headers=auth(),
                        json={"username": "carol", "password": "abc", "role": "user"})
    assert short.status_code == 400 and short.json()["error_code"] == 4001  # 密码 min4


def test_user_reset_password_revokes_sessions(settings_factory, monkeypatch):
    store = FakeUsersStore()
    store.reset_to["bob"] = 3
    store.users.append({"username": "bob", "role": "user", "disabled": False,
                        "created_at": "-", "sessions": 3})
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    r = client.post("/api/system/users/bob/reset-password", headers=auth(),
                    json={"password": "new-pw"})
    assert r.status_code == 200
    assert r.json() == {"reset": True, "sessions_revoked": 3}
    missing = client.post("/api/system/users/ghost/reset-password", headers=auth(),
                          json={"password": "new-pw"})
    assert missing.status_code == 404 and missing.json()["error_code"] == 4041


def test_user_disable_self_guarded_4001(settings_factory, monkeypatch):
    store = FakeUsersStore()
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    r = client.post("/api/system/users/admin1/disable", headers=auth())
    assert r.status_code == 400 and r.json()["error_code"] == 4001
    assert store.disabled == []  # 防呆：请求根本没到 store
    ghost = client.post("/api/system/users/ghost/enable", headers=auth())
    assert ghost.status_code == 404 and ghost.json()["error_code"] == 4041


def test_user_disable_enable_flow(settings_factory, monkeypatch):
    store = FakeUsersStore()
    store.users.append({"username": "bob", "role": "user", "disabled": False,
                        "created_at": "-", "sessions": 0})
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    assert client.post("/api/system/users/bob/disable", headers=auth()).status_code == 200
    assert store.disabled == [("bob", True)]
    users = client.get("/api/system/users", headers=auth()).json()["users"]
    bob = next(u for u in users if u["username"] == "bob")
    assert bob["disabled"] is True
    assert client.post("/api/system/users/bob/enable", headers=auth()).status_code == 200
    assert store.disabled == [("bob", True), ("bob", False)]


def test_guest_toggle_immediate_effect(settings_factory, monkeypatch):
    store = FakeUsersStore()
    client = admin_client(monkeypatch, settings_factory, store, FakePg())
    r = client.post("/api/system/settings/guest-enabled", headers=auth(),
                    json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"guest_enabled": False}
    assert store.guest_toggles == [False]
    # 即刻生效：在线游客会话被中间件拒绝（resolve 后复核 guest_enabled）
    guest_ident = Identity("guest", None, "guest", "tg")
    store.sessions["tg"] = guest_ident
    blocked = client.get("/api/conversations", headers={"Authorization": "Bearer tg"})
    assert blocked.status_code == 403 and blocked.json()["error_code"] == 4031


# ── 全库会话管理（旁路）──
def test_all_conversations_bypass_visibility(settings_factory, monkeypatch):
    """admin 全库列表含机器域与他人会话行（可见域旁路的形状证明）。"""
    client = admin_client(monkeypatch, settings_factory, FakeUsersStore(), FakePg())
    r = client.get("/api/system/conversations", headers=auth())
    assert r.status_code == 200
    rows = r.json()["conversations"]
    assert len(rows) == 2
    machine = next(row for row in rows if row["owner"] is None)
    assert machine["has_reply"] is True and machine["user_id"] is None
    bobs = next(row for row in rows if row["owner"] == "bob")
    assert bobs["has_reply"] is False
    q = client.get("/api/system/conversations", headers=auth(), params={"q": "bob"})
    assert [c["title"] for c in q.json()["conversations"]] == ["bob 的会话"]


def test_admin_bypass_delete_other_users_conversation(settings_factory, monkeypatch):
    pg = FakePg()
    client = admin_client(monkeypatch, settings_factory, FakeUsersStore(), pg)
    target = next(c for c in pg.conversations if c["owner"] == "bob")["id"]
    r = client.delete(f"/api/system/conversations/{target}", headers=auth())
    assert r.status_code == 200 and r.json() == {"deleted": True, "id": target}
    assert pg.deleted == [target]
    missing = client.delete(f"/api/system/conversations/{uuid.uuid4()}", headers=auth())
    assert missing.status_code == 404 and missing.json()["error_code"] == 4041
    nonuuid = client.delete("/api/system/conversations/not-a-uuid", headers=auth())
    assert nonuuid.status_code == 404 and nonuuid.json()["error_code"] == 4041


def test_purge_empty_dry_run_then_execute(settings_factory, monkeypatch):
    pg = FakePg()
    client = admin_client(monkeypatch, settings_factory, FakeUsersStore(), pg)
    preview = client.post("/api/system/conversations/purge-empty", headers=auth(),
                          json={"dry_run": True})
    assert preview.status_code == 200
    body = preview.json()
    assert body["dry_run"] is True and body["count"] == 2
    assert len(body["samples"]) == 2
    assert pg.purged is False  # 预览不删
    execute = client.post("/api/system/conversations/purge-empty", headers=auth(),
                          json={"dry_run": False})
    assert execute.status_code == 200 and execute.json() == {"dry_run": False, "deleted": 2}
    assert pg.purged is True


def test_purge_empty_default_is_dry_run(settings_factory, monkeypatch):
    """dry_run 缺省 true（先预览再删，prune 惯例）。"""
    pg = FakePg()
    client = admin_client(monkeypatch, settings_factory, FakeUsersStore(), pg)
    body = client.post("/api/system/conversations/purge-empty", headers=auth(),
                       json={}).json()
    assert body["dry_run"] is True and pg.purged is False
