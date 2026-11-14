"""鉴权契约（docs/01 §3.1/§3.2）：4011 + health 豁免 + 开发模式放行 + 用户体系三角色。

armed 模式全部用 FakeUsersStore（monkeypatch build_users_store），绝不依赖真实 PG；
会话隔离的真实 PG 集成例在 api_test/test_conversations_api.py。
"""

from fastapi.testclient import TestClient

import bidqa.api.auth as auth_mod
import bidqa.api.middleware as mw
from bidqa.auth.identity import Identity
from bidqa.main import create_app

TOKEN = "secret-token"


class FakeUsersStore:
    """armed 模式替身：resolve/logout/guest_enabled/login 覆盖中间件与 auth 端点所需。"""

    def __init__(self, sessions: dict[str, Identity] | None = None, guest_on: bool = True):
        self.sessions = dict(sessions or {})
        self.guest_on = guest_on
        self.logins: list[tuple[str, str]] = []
        self.logged_out: list[str] = []

    def resolve(self, token: str) -> Identity | None:
        return self.sessions.get(token)

    def guest_enabled(self) -> bool:
        return self.guest_on

    def login(self, username: str, password: str, ttl_hours: int):
        self.logins.append((username, password))
        if username == "alice" and password == "pw":
            return {"token": "t-alice", "username": "alice", "role": "user"}
        return None

    def login_guest(self, ttl_hours: int):
        if not self.guest_on:
            return None
        return {"token": "t-guest", "username": "guest", "role": "guest"}

    def logout(self, token: str) -> bool:
        self.logged_out.append(token)
        return token in self.sessions or token == "t-alice"


def patch_store(monkeypatch, store: FakeUsersStore) -> None:
    """中间件与 auth 端点两处 import 点同时替换。"""
    monkeypatch.setattr(mw, "build_users_store", lambda: store)
    monkeypatch.setattr(auth_mod, "build_users_store", lambda: store)


def armed_client(monkeypatch, settings_factory, store: FakeUsersStore, **extra) -> TestClient:
    settings = settings_factory(USERS_ENABLED="true", **extra)
    patch_store(monkeypatch, store)
    return TestClient(create_app(settings))


# ── 旧契约（单机 token 模式）──
def test_health_exempt_even_with_token(settings_factory):
    client = TestClient(create_app(settings_factory(API_AUTH_TOKEN=TOKEN)))
    assert client.get("/api/health").status_code == 200


def test_missing_token_rejected_4011(settings_factory):
    """中间件先于路由执行：未带 token 访问任意 /api 路径 → 401/4011。"""
    client = TestClient(create_app(settings_factory(API_AUTH_TOKEN=TOKEN)))
    r = client.get("/api/whatever")
    assert r.status_code == 401
    body = r.json()
    assert body["error_code"] == 4011
    for key in ("message", "trace_id", "detail"):
        assert key in body


def test_wrong_token_rejected_4011(settings_factory):
    client = TestClient(create_app(settings_factory(API_AUTH_TOKEN=TOKEN)))
    r = client.get("/api/whatever", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["error_code"] == 4011


def test_options_preflight_not_401(settings_factory):
    """CORS 预检不带 Authorization：必须穿透鉴权层由 CORS 应答，绝不 401。

    回归（2026-11-09）：中间件注册顺序曾让 OPTIONS 先撞鉴权 → 401，
    浏览器侧所有跨域请求（含流式问答）全断；curl/e2e 不走预检故未抓到。
    """
    client = TestClient(create_app(settings_factory(API_AUTH_TOKEN=TOKEN)))
    r = client.options("/api/chat/stream",
                       headers={"Origin": "http://localhost:3000",
                                "Access-Control-Request-Method": "POST",
                                "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_correct_token_reaches_routing(settings_factory):
    client = TestClient(create_app(settings_factory(API_AUTH_TOKEN=TOKEN)))
    r = client.get("/api/whatever", headers={"Authorization": f"Bearer {TOKEN}"})
    # 鉴权通过 → 进入路由 → 未注册路径 → 统一错误体 4041
    assert r.status_code == 404
    assert r.json()["error_code"] == 4041


def test_dev_mode_allows_all(settings_factory):
    client = TestClient(create_app(settings_factory()))  # 未配置 token
    assert client.get("/api/whatever").status_code == 404  # 直接进路由


def test_legacy_mode_ignores_users_store(settings_factory, monkeypatch):
    """USERS_ENABLED=false：不查用户体系，不设身份（旧单机模式回归）。"""
    store = FakeUsersStore()
    patch_store(monkeypatch, store)
    client = TestClient(create_app(settings_factory()))  # CORE_ENV 默认 false
    assert client.get("/api/whatever").status_code == 404
    assert store.logins == [] and not store.logged_out


# ── armed 模式：认证 ──
def test_armed_bare_request_4011(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore())
    r = client.get("/api/whatever")
    assert r.status_code == 401
    assert r.json()["error_code"] == 4011


def test_armed_bad_token_4011(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore())
    r = client.get("/api/whatever", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.json()["error_code"] == 4011


def test_armed_valid_session_reaches_routing(settings_factory, monkeypatch):
    ident = Identity("user", "u-1", "alice", "t1")
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore({"t1": ident}))
    r = client.get("/api/whatever", headers={"Authorization": "Bearer t1"})
    assert r.status_code == 404  # 通过中间件 → 路由 4041
    assert r.json()["error_code"] == 4041


def test_machine_token_is_admin_level(settings_factory, monkeypatch):
    """主 token 在 armed 模式仍是管理员级：可进 metrics 门禁（docs/01 §3.1）。"""
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore(), API_AUTH_TOKEN=TOKEN)
    r = client.get("/api/system/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_store_unavailable_degrades_open(settings_factory, monkeypatch):
    """PG 不可用（store=None）：armed 模式放行（降级诚实；登录端点此时同样 503）。"""
    monkeypatch.setattr(mw, "build_users_store", lambda: None)
    monkeypatch.setattr(auth_mod, "build_users_store", lambda: None)
    client = TestClient(create_app(settings_factory(USERS_ENABLED="true")))
    assert client.get("/api/whatever").status_code == 404


# ── armed 模式：角色门禁 ──
def test_user_forbidden_on_system_metrics(settings_factory, monkeypatch):
    ident = Identity("user", "u-1", "alice", "t1")
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore({"t1": ident}))
    r = client.get("/api/system/metrics", headers={"Authorization": "Bearer t1"})
    assert r.status_code == 403
    assert r.json()["error_code"] == 4031


def test_user_ok_on_system_providers(settings_factory, monkeypatch):
    """providers 供档位下拉：全员可达（门禁只锁 metrics/logs）。"""
    ident = Identity("user", "u-1", "alice", "t1")
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore({"t1": ident}))
    assert client.get("/api/system/providers",
                      headers={"Authorization": "Bearer t1"}).status_code == 200


def test_guest_blocked_on_vision(settings_factory, monkeypatch):
    ident = Identity("guest", None, "guest", "tg")
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore({"tg": ident}))
    r = client.post("/api/vision", json={"image": "x" * 64},
                    headers={"Authorization": "Bearer tg"})
    assert r.status_code == 403
    assert r.json()["error_code"] == 4031


def test_guest_blocked_on_conversations(settings_factory, monkeypatch):
    ident = Identity("guest", None, "guest", "tg")
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore({"tg": ident}))
    r = client.get("/api/conversations", headers={"Authorization": "Bearer tg"})
    assert r.status_code == 403
    assert r.json()["error_code"] == 4031


def test_guest_disabled_mid_session_403(settings_factory, monkeypatch):
    """游客开关运行时关闭：既有游客会话即刻被拒（docs/01 §3.1）。"""
    ident = Identity("guest", None, "guest", "tg")
    store = FakeUsersStore({"tg": ident}, guest_on=False)
    client = armed_client(monkeypatch, settings_factory, store)
    r = client.get("/api/whatever", headers={"Authorization": "Bearer tg"})
    assert r.status_code == 403
    assert r.json()["error_code"] == 4031


# ── auth 端点 ──
def test_login_success(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore())
    r = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert r.status_code == 200
    assert r.json() == {"token": "t-alice", "username": "alice", "role": "user"}


def test_login_wrong_credentials_4011(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore())
    r = client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    assert r.status_code == 401
    assert r.json()["error_code"] == 4011


def test_guest_login_ok(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore())
    r = client.post("/api/auth/guest")
    assert r.status_code == 200
    assert r.json()["role"] == "guest"


def test_guest_login_closed_4031(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore(guest_on=False))
    r = client.post("/api/auth/guest")
    assert r.status_code == 403
    assert r.json()["error_code"] == 4031


def test_me_unauthenticated_shape(settings_factory, monkeypatch):
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore())
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["users_enabled"] is True
    assert body["guest_enabled"] is True


def test_me_with_session(settings_factory, monkeypatch):
    ident = Identity("user", "u-1", "alice", "t1")
    client = armed_client(monkeypatch, settings_factory, FakeUsersStore({"t1": ident}))
    body = client.get("/api/auth/me", headers={"Authorization": "Bearer t1"}).json()
    assert body["authenticated"] is True
    assert body["username"] == "alice" and body["role"] == "user"


def test_me_legacy_mode_reports_disabled(settings_factory):
    client = TestClient(create_app(settings_factory()))
    body = client.get("/api/auth/me").json()
    assert body == {"authenticated": False, "users_enabled": False, "guest_enabled": False}


def test_logout_revokes_session(settings_factory, monkeypatch):
    ident = Identity("user", "u-1", "alice", "t1")
    store = FakeUsersStore({"t1": ident})
    client = armed_client(monkeypatch, settings_factory, store)
    r = client.post("/api/auth/logout", headers={"Authorization": "Bearer t1"})
    assert r.status_code == 200 and r.json()["logged_out"] is True
    assert store.logged_out == ["t1"]
    assert client.get("/api/whatever",
                      headers={"Authorization": "Bearer t1"}).status_code == 404
