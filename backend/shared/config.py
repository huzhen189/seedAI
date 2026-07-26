"""Shared core config — single source of truth for business & agent.

Both services import this. Secrets come from the repo-root .env via pydantic
BaseSettings. Async SQLAlchemy MySQL driver is enforced (mysql+aiomysql) so
local runs hit the same Cloud MySQL as docker (the historical NAT-kill bug is
avoided via pool_pre_ping + pool_recycle in db.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # backend/shared/config.py -> repo root
ENV_FILE = _PROJECT_ROOT / ".env"

logger = logging.getLogger("shared.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), extra="ignore")

    # ---- service discovery ----
    business_api_port: int = 7101
    ai_service_port: int = 7102
    ai_servers: str = ""  # comma-separated agent base urls (empty => self/local)
    ai_server_health_path: str = "/healthz"

    # ---- data layer ----
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "sqlite+aiosqlite:///./seedai.db"
    mysql_url: str = ""
    chroma_url: str = "http://chroma:8000"
    chroma_api_version: str = "v2"

    # ---- auth ----
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl: int = 3600

    # ---- model gateways ----
    deepseek_api_key: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    hy3_api_key: str = ""
    hy3_base_url: str = "https://tokenhub.tencentmaas.com/v1"
    hy3_model: str = "hy3"
    qwen_embedding_key: str = ""

    # ---- qwen api host (ali maas, optional) ----
    qwen_api_host: str = ""

    # ---- conversation / context ----
    chat_recent_redis_ttl: int = 1800       # 30min sliding window for recent context cache
    chat_recent_limit: int = 10             # recent N messages (was hard-coded LIMIT 5)
    conversation_summary_ttl: int = 1800    # summary sliding window

    # ---- tracing / orchestration ----
    worker_pool_size: int = 4
    stream_event_ttl: int = 3600            # gen:stream:<tid> retention
    trace_replay_after_max: int = 100000

    # ---- cross-service connect token (business -> agent) ----
    ai_connect_token_ttl: int = 300         # 5min short-lived direct-connect token

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


settings = Settings()
