"""配置体系（docs/01 §5）：.env 加载 + 核心/能力两级启动校验。

优先级：真实环境变量 > .env 配置文件 > 内置默认值（pydantic-settings 默认序，
等价于 load_dotenv(override=False) 语义——真实环境变量永远胜出）。

降级规则：能力型配置缺失 = 启动成功 + health 显示降级；核心类缺失 = 启动失败
（无主模型/向量库无法服务）。进程内状态（限流/在线评测/日志缓冲）假定单进程，
workers=1 是硬性约定（docs/01 §6）。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """核心配置缺失/非法（启动失败）。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── 核心槽位（缺失=启动失败；通用 OpenAI-compatible 自填，无预置厂商枚举）──
    CHAT_BASE_URL: str = ""
    CHAT_API_KEY: str = ""
    CHAT_MODEL: str = ""
    QDRANT_URL: str = ""
    QDRANT_COLLECTION: str = "bidqa_knowledge_v1"  # 版本化集合名（docs/01 §4.1）
    QDRANT_API_KEY: str = ""

    # ── 能力槽位（缺失=对应能力降级，启动成功）──
    CHAT_THINKING_MODEL: str = ""  # 留空=深度思考回退提示词注入
    CHAT_PROFILES: str = ""  # 主槽位可选多组命名 profile（逗号分隔），配套 CHAT_<name>_* 三件套
    CHAT_MAX_TOKENS: int | None = None  # 主槽位输出预算；留空=不传（用上游默认）
    VISION_BASE_URL: str = ""
    VISION_API_KEY: str = ""
    VISION_MODEL: str = ""
    JUDGE_BASE_URL: str = ""  # 配置后评测执行前指纹异源校验（完全相同→5032）
    JUDGE_API_KEY: str = ""
    JUDGE_MODEL: str = ""
    EMBEDDING_BASE_URL: str = ""  # 缺失降级=本地 jieba BM25 灾备通道（sparse_bm25）
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = ""
    RERANK_BASE_URL: str = ""  # 缺失降级=跳过精排直取 RRF 融合结果
    RERANK_API_KEY: str = ""
    RERANK_MODEL: str = ""
    NEO4J_URI: str = "bolt://127.0.0.1:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""
    POSTGRES_HOST: str = "127.0.0.1"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "bidqa"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = ""
    TAVILY_API_KEY: str = ""
    EXA_API_KEY: str = ""
    WEB_SEARCH_PROVIDERS: str = ""  # 联网供应商名册（逗号分隔 tavily/exa）；留空=联网关闭，前端不显示联网按钮
    MINERU_API_KEY: str = ""  # 缺失=PDF 走 pdfplumber 本地兜底
    API_AUTH_TOKEN: str = ""  # 留空=单机开发模式放行
    API_CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    ONLINE_EVAL_ENABLED: bool = False
    ONLINE_EVAL_SAMPLE_RATE: float = 0.0  # 默认 0：行为指标全量免费，点踩 100% 必评
    SUBJECT_BATCH_API_KEY: str = ""  # 数据加工批量通道（subject-batch）；缺失=run 不可用，build/fetch 不受影响
    SUBJECT_BATCH_BASE_URL: str = "https://api.z.ai/api/paas/v4"  # 国际版 z.ai（无 Batch API，走同步并发）
    SUBJECT_BATCH_MODEL: str = "glm-4.7-flash"  # 免费档（GLM-4.7-Flash，定价页核实 Free）

    # ── 用户体系（三角色 admin/user/guest；docs/01 §3.1 用户体系段）──
    USERS_ENABLED: bool = True  # false=退回单机 API_AUTH_TOKEN 模式（旧测试/脚本兼容开关）
    ADMIN_USERNAME: str = "admin"  # 首次启动 users 空表时的种子管理员
    ADMIN_PASSWORD: str = "admin123"  # 仅用于首次种子；登录后请 `bidqa user reset` 改密
    SESSION_TTL_HOURS: int = 72  # 登录会话有效期（opaque token 存 sessions 表）
    GUEST_RATE_LIMIT: int = 10  # 游客独立限流配额（次/60s；普通用户 30/60s 不变）

    # ── 核心校验（启动失败路径）──
    def missing_core(self) -> list[str]:
        core = {
            "CHAT_BASE_URL": self.CHAT_BASE_URL,
            "CHAT_API_KEY": self.CHAT_API_KEY,
            "CHAT_MODEL": self.CHAT_MODEL,
            "QDRANT_URL": self.QDRANT_URL,
        }
        return [key for key, value in core.items() if not value.strip()]

    def check_worker_count(self) -> None:
        """workers=1 防呆（docs/01 §6）：检测到多 worker 环境变量即拒绝启动。"""
        raw = os.environ.get("WEB_CONCURRENCY", "").strip()
        if raw.isdigit() and int(raw) > 1:
            raise ConfigError(
                f"WEB_CONCURRENCY={raw}：本项目 workers=1 是硬性约定"
                "（限流/在线评测/日志缓冲等进程内状态假定单进程）"
            )

    def validate_core(self) -> None:
        self.check_worker_count()
        missing = self.missing_core()
        if missing:
            raise ConfigError(
                "核心配置缺失，无法启动（docs/01 §5）："
                + ", ".join(missing)
                + "。请复制 backend/.env.example 为 backend/.env 并填写三槽位。"
            )

    # ── 能力降级标志（True=已配置；实际连通性由 /api/health 探测，M1-9）──
    @property
    def has_chat(self) -> bool:
        """主槽位三件套齐备，或任一命名档齐备（health capabilities 口径：已配置）。"""
        return bool(self.CHAT_BASE_URL.strip() and self.CHAT_API_KEY.strip()
                    and self.CHAT_MODEL.strip()) or bool(self.chat_profiles())

    @property
    def has_thinking(self) -> bool:
        return bool(self.CHAT_THINKING_MODEL.strip())

    @property
    def has_vision(self) -> bool:
        return all([self.VISION_BASE_URL.strip(), self.VISION_API_KEY.strip(), self.VISION_MODEL.strip()])

    @property
    def has_judge(self) -> bool:
        return all([self.JUDGE_BASE_URL.strip(), self.JUDGE_API_KEY.strip(), self.JUDGE_MODEL.strip()])

    @property
    def has_embedding(self) -> bool:
        return all([self.EMBEDDING_BASE_URL.strip(), self.EMBEDDING_API_KEY.strip(), self.EMBEDDING_MODEL.strip()])

    @property
    def has_rerank(self) -> bool:
        return all([self.RERANK_BASE_URL.strip(), self.RERANK_API_KEY.strip(), self.RERANK_MODEL.strip()])

    @property
    def has_neo4j(self) -> bool:
        return bool(self.NEO4J_URI.strip() and self.NEO4J_PASSWORD.strip())

    @property
    def has_postgres(self) -> bool:
        return bool(self.POSTGRES_HOST.strip() and self.POSTGRES_DB.strip())

    @property
    def has_web_search(self) -> bool:
        return bool(self.web_search_providers())  # 名册∩key 双门槛（EXA 单配不再假阳性）

    @property
    def has_pdf_api(self) -> bool:
        return bool(self.MINERU_API_KEY.strip())

    @property
    def has_subject_batch(self) -> bool:
        return bool(self.SUBJECT_BATCH_API_KEY.strip())

    @property
    def auth_enabled(self) -> bool:
        return bool(self.API_AUTH_TOKEN.strip())

    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.API_CORS_ORIGINS.split(",") if o.strip()]

    def web_search_providers(self) -> list[str]:
        """D4/D5 联网供应商名册：WEB_SEARCH_PROVIDERS 逗号切分，仅收 tavily/exa。

        名册内还须对应 key 已配才保留（列名缺 key=该供应商跳过）——配置序即检索合并序。
        """
        keys = {"tavily": self.TAVILY_API_KEY, "exa": self.EXA_API_KEY}
        return [p for p in (x.strip().lower() for x in self.WEB_SEARCH_PROVIDERS.split(","))
                if p in keys and keys[p].strip()]

    def chat_profiles(self) -> dict[str, dict[str, str]]:
        """A12/C8：CHAT_PROFILES 命名档 → {name: {base_url, api_key, model}}。

        档位三件套 CHAT_<NAME>_BASE_URL/_API_KEY/_MODEL 读真实环境变量 + .env
        （pydantic-settings 不加载未声明字段，这里手动查）；缺任一件的档位跳过。
        """
        from dotenv import dotenv_values

        names = [p.strip() for p in self.CHAT_PROFILES.split(",") if p.strip()]
        if not names:
            return {}
        file_vals: dict[str, str] = {}
        env_file = self.model_config.get("env_file")
        if env_file:
            file_vals = {k: v for k, v in (dotenv_values(env_file) or {}).items()
                         if isinstance(v, str)}

        def lookup(suffix: str) -> str:
            name = f"CHAT_{suffix}"
            return os.environ.get(name) or file_vals.get(name) or ""

        out: dict[str, dict[str, str]] = {}
        for name in names:
            upper = name.upper()
            profile = {
                "base_url": lookup(f"{upper}_BASE_URL"),
                "api_key": lookup(f"{upper}_API_KEY"),
                "model": lookup(f"{upper}_MODEL"),
            }
            if all(profile.values()):
                out[name] = profile
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
