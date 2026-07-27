"""Shared core config — single source of truth for the unified single-process app.

Both the business (BFF/asset/auth/admin/analytics/metrics) layer and the agent
(inference) layer import this one ``Settings``. Secrets come from the repo-root
``.env`` via pydantic BaseSettings. Async SQLAlchemy MySQL driver is enforced
(mysql+aiomysql) so local runs hit the same Cloud MySQL as docker (the historical
NAT-kill bug is avoided via pool_pre_ping + pool_recycle in app/db.py).

This merges what used to be two separate settings objects (business/app/config.py
and ai_service/app/config.py) so the two services now run in one process.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # backend/shared/config.py -> repo root
ENV_FILE = _PROJECT_ROOT / ".env"

logger = logging.getLogger("shared.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), extra="ignore")

    # ---- service identity (single process now) ----
    app_title: str = "SeedAI API"
    business_api_port: int = 7101
    ai_service_port: int = 7102
    app_port: int = 7101  # the unified process listens here (merges the two)
    # legacy field kept so old references keep resolving
    ai_servers: str = ""  # comma-separated agent base urls (empty => self/local)
    ai_server_health_path: str = "/healthz"
    # kept for backward-compat; points at this same process now
    ai_service_url: str = "http://localhost:7101"
    business_service_url: str = "http://localhost:7101"

    # ---- data layer ----
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "sqlite+aiosqlite:///./seedai.db"
    mysql_url: str = ""
    chroma_url: str = "http://chroma:8000"
    chroma_api_version: str = "v2"
    dev_memory_queue: bool = False  # local dev: in-process queue instead of Redis Stream

    # ---- auth ----
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 7 * 24 * 3600
    seed_super_admin: str = ""  # username to promote to super_admin on startup

    # ---- model gateways ----
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.7-plus"
    qwen_embedding_key: str = ""
    qwen_embedding_model: str = "text-embedding-v3"
    # 嵌入专用 base_url: 本机 Qwen 嵌入 key(sk-ws-)属 ws 私有 MaaS 工作区主机签发,
    # 与聊天 token-plan 主机(sk-sp-)不同; 复用 qwen_base_url 打错工作区端点会 401。
    qwen_embedding_base_url: str = "https://ws-rao72of9tmiy6llq.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    hy3_api_key: str = ""
    hy3_api_key_demo: str = ""
    hy3_base_url: str = "https://tokenhub.tencentmaas.com/v1"
    hy3_model: str = "hy3"
    default_model: str = "qwen"  # user's chosen default (Qwen)

    # ---- conversation / context ----
    chat_recent_redis_ttl: int = 1800       # 30min sliding window for recent context cache
    chat_recent_limit: int = 10             # recent N messages (was hard-coded LIMIT 5)
    conversation_summary_ttl: int = 1800    # summary sliding window
    cache_user_ttl: int = 9000              # 150 min

    # ---- tracing / orchestration ----
    worker_pool_size: int = 4
    worker_concurrency: int = 2
    fallback_order: str = "hy3,qwen,deepseek"
    stream_event_ttl: int = 3600            # gen:stream:<tid> retention
    trace_replay_after_max: int = 100000

    # ---- cross-service connect token (legacy; same-process now) ----
    ai_connect_token_ttl: int = 300

    # ---- quota (free 50/day; per-plan map) ----
    free_daily_quota: int = 50
    plan_daily_quota: dict = {"free": 50, "pro": 500, "enterprise": 5000}

    # ---- site / cookie / cors (single-process, single domain) ----
    site_domain: str = "seedai.huzhen.net.cn"
    preview_domain: str = "seedhtml.huzhen.net.cn"
    cookie_domain: str = ""
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:7100,http://seedai.huzhen.net.cn:7100,http://seedai.huzhen.net.cn,https://seedai.huzhen.net.cn"

    # ---- Chroma collections (9) ----
    chroma_collection_components: str = "components"
    chroma_collection_memory: str = "memory"
    chroma_collection_cache: str = "cache_gen"
    chroma_collection_user_preferences: str = "user_preferences"
    chroma_collection_project_memory: str = "project_memory"
    chroma_collection_project_code: str = "project_code"
    chroma_collection_error_patterns: str = "error_patterns"
    chroma_collection_intents: str = "intents"
    rag_top_k: int = 5

    # ---- 意图识别阈值(混合级联, 集中可调, 单一来源) ----
    intent_super_fast: float = 0.90        # 强规则 + 向量 top1 相似度 ≥ 此值 → 跳过 LLM
    intent_novelty: float = 0.45           # top5 最高相似度 < 此值 且无规则命中 → 闲聊兜底
    intent_commit: float = 0.80            # 置信度 ≥ 此值 → 直接路由
    intent_clarify_lo: float = 0.45        # 低于此值进入澄清/兜底判定
    intent_clarify_max_rounds: int = 2     # 最多追问轮次(≤2)
    intent_top_k: int = 5                  # 向量召回 top-k(R2: was hard LIMIT 5)

    # ---- 多意图 A+B 路由 ----
    split_b_enabled: bool = True
    split_b_max_subtasks: int = 6
    split_escalate_low_conf: float = 0.6
    split_repair_max_rounds: int = 2

    # ---- 后置质检(QC) ----
    qc_judges: str = "deepseek,qwen,hy3"
    qc_needs_review_variance: float = 4.0
    qc_timeout_seconds: float = 60.0
    qc_fix_enabled: bool = True
    qc_fix_max_rounds: int = 2

    # ---- COS / objects ----
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = "seedhtml-1252059540"
    cos_region: str = "ap-guangzhou"
    cos_preview_domain: str = "https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com"
    cos_base_path: str = "previews"
    cos_ttl_days: int = 0
    artifact_dir: str = "./artifacts"

    # ---- 检索 / 搜索 ----
    tavily_api_key: str = ""
    serper_api_key: str = ""
    web_search_top_k: int = 5

    # ---- 图像生成 ----
    image_api_key: str = ""
    image_api_base: str = ""
    image_model: str = "dall-e-3"

    @model_validator(mode="after")
    def _post(self) -> "Settings":
        # legacy MYSQL_URL -> database_url (only when still sqlite default)
        if self.database_url.startswith("sqlite") and self.mysql_url:
            self.database_url = self.mysql_url
        # enforce async MySQL driver so local==docker==cloud
        u = self.database_url
        if u.startswith("mysql+pymysql://"):
            u = "mysql+aiomysql://" + u[len("mysql+pymysql://"):]
        elif u.startswith("mysql://"):
            u = "mysql+aiomysql://" + u[len("mysql://"):]
        self.database_url = u
        if "mysql" in self.database_url and "charset=" not in self.database_url:
            sep = "&" if "?" in self.database_url else "?"
            self.database_url += f"{sep}charset=utf8mb4"
        return self

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        # refuse to start in production with default secret
        if self.jwt_secret == "dev-secret-change-me" and os.environ.get("ENV", "") not in ("dev", "local", ""):
            raise RuntimeError(
                "FATAL: JWT_SECRET 仍为默认值,生产环境拒绝启动。请在 .env 中覆盖为随机强值。"
            )
        return self


settings = Settings()
