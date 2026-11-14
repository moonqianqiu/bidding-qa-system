"""Bearer 鉴权中间件（docs/01 §3.1）：机器主 token + 用户体系三角色（admin/user/guest）。

解析顺序：①机器主 token（API_AUTH_TOKEN 配置时，语义=管理员级，脚本/e2e 免登录，
不经过用户体系）→ ②USERS_ENABLED=false：旧单机模式（配置 token 时校验，否则放行）
→ ③用户体系 armed：sessions 表解析会话身份；store 不可用（PG 不可用）→ 无法验证
会话，放行（降级诚实：此时登录端点同样 503，见 docs/01 §3.1）。

豁免清单：/api/health（探测）+ /api/auth/login|guest|me（登录引导）+ OPTIONS（CORS 预检）。
路径门禁（armed 时）：/api/system/{console,users,conversations,settings,metrics,logs}
前缀仅 admin（前缀匹配——users/动态子路径与 purge-empty 等动作全被覆盖；
/api/system/providers 不在前缀内，供普通用户档位下拉，全员可达）；
/api/vision 与 /api/conversations* 拒绝 guest。身份存 request.state.identity
供端点做数据隔离。401 走 4011；角色不足/游客开关已关走 403/4031（统一错误体）。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from bidqa.auth_core import MACHINE_USERNAME, ROLE_ADMIN, ROLE_GUEST, Identity
from bidqa.errors import EC_FORBIDDEN, EC_UNAUTHORIZED, error_body
from bidqa.service import build_users_store

HEALTH_PATH = "/api/health"  # 免鉴权（docs/01 §3.1）
AUTH_EXEMPT_PATHS = (HEALTH_PATH, "/api/auth/login", "/api/auth/guest", "/api/auth/me")
# 控制台域（A15/B11/B12）：前缀匹配，动态子路径一并覆盖
ADMIN_ONLY_PREFIXES = ("/api/system/console", "/api/system/users",
                       "/api/system/conversations", "/api/system/settings",
                       "/api/system/metrics", "/api/system/logs")
GUEST_BLOCKED_PREFIXES = ("/api/vision", "/api/conversations")


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401,
                        content=error_body(EC_UNAUTHORIZED, "Bearer token 缺失/无效"))


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(status_code=403, content=error_body(EC_FORBIDDEN, message))


def _role_gate(path: str, role: str) -> JSONResponse | None:
    """armed 模式路径门禁：/api/system 控制台前缀仅 admin；guest 禁入 vision/conversations。"""
    if any(path.startswith(p) for p in ADMIN_ONLY_PREFIXES) and role != ROLE_ADMIN:
        return _forbidden("需要管理员角色")
    if role == ROLE_GUEST and path.startswith(GUEST_BLOCKED_PREFIXES):
        return _forbidden("游客模式不可用")
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str, users_enabled: bool = True):
        super().__init__(app)
        self.token = token
        self.users_enabled = users_enabled

    async def dispatch(self, request: Request,
                       call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in AUTH_EXEMPT_PATHS:
            return await call_next(request)
        if request.method == "OPTIONS":
            # CORS 预检不带 Authorization：交给 CORS 层/路由应答，不按 401 处理
            #（鉴权中间件在 CORS 内层时，预检先到这里——2026-11-09 修复）
            return await call_next(request)
        auth = request.headers.get("authorization", "")

        # ① 机器主 token：管理员级（脚本/e2e 免登录；user_id=None=机器域）
        if self.token and auth == f"Bearer {self.token}":
            request.state.identity = Identity(ROLE_ADMIN, None, MACHINE_USERNAME)
            return await call_next(request)

        # ② 用户体系关闭：旧单机模式（配置 token 时校验，否则放行）
        if not self.users_enabled:
            if self.token:
                return _unauthorized()
            return await call_next(request)

        # ③ 用户体系 armed：sessions 解析会话身份
        store = build_users_store()
        if store is None:  # PG 不可用：无法验证会话，放行（降级诚实，登录端点此时同样不可用）
            return await call_next(request)
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        identity = store.resolve(token) if token else None
        if identity is None:
            return _unauthorized()
        if identity.role == ROLE_GUEST and not store.guest_enabled():
            return _forbidden("游客模式已关闭")  # 开关即刻生效：既有游客会话一并拒绝
        gate = _role_gate(path, identity.role)
        if gate is not None:
            return gate
        request.state.identity = identity
        return await call_next(request)
