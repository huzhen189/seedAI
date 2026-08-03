"""SeedAI 生产配置单一真相源。

环境变量承载密钥与部署差异，YAML 承载模型档位、路由阈值和租户配额声明。
模块导入阶段完成结构校验；生产环境对密钥、数据库、CORS 与加密配置 fail-fast。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.core.contracts import MAX_ACTION_ITEMS  # noqa: E402  # 硬上限校验 le 用，单向依赖不形成循环


CONFIG_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"

StageId = Literal["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"]
ModelSlotName = Literal[
    "intent_lite",
    "intent_strong",
    "exec_standard",
    "exec_pro",
    "exec_ultra",
]
ProviderName = Literal["qwen", "hy3", "deepseek"]
BillingMode = Literal["token_plan", "metered"]
TierName = Literal["free", "pro", "max"]


class ConfigLoadError(RuntimeError):
    """配置文件缺失、格式错误或契约校验失败。"""


class ModelBinding(BaseModel):
    provider: ProviderName
    model_id: str = Field(min_length=1)
    base_url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    default_base_url: HttpUrl
    billing: BillingMode
    tenant_selectable: bool
    stages: list[StageId] = Field(min_length=1)


class EmbeddingBinding(BaseModel):
    provider: Literal["qwen"]
    model_id: Literal["text-embedding-v3"]
    base_url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    default_base_url: HttpUrl
    dimensions: Literal[1024]
    cloud_only: Literal[True]


class ModelsConfig(BaseModel):
    version: Literal[1]
    slots: dict[str, ModelBinding]
    embedding: EmbeddingBinding

    @model_validator(mode="after")
    def validate_slot_contract(self) -> ModelsConfig:
        expected = {
            "intent_lite",
            "intent_strong",
            "exec_standard",
            "exec_pro",
            "exec_ultra",
        }
        actual = set(self.slots)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(f"模型档位必须精确为五档，missing={missing}, extra={extra}")
        for name in ("intent_lite", "intent_strong"):
            binding = self.slots[name]
            if binding.tenant_selectable:
                raise ValueError(f"{name} 是平台固定档，不允许 tenant_selectable")
            if "S6" in binding.stages:
                raise ValueError(f"{name} 不允许绑定 S6")
        for name in ("exec_standard", "exec_pro", "exec_ultra"):
            binding = self.slots[name]
            if not binding.tenant_selectable:
                raise ValueError(f"{name} 必须允许租户选择")
            if binding.stages != ["S6"]:
                raise ValueError(f"{name} 只能绑定 S6，实际为 {binding.stages}")
        return self

    def slot(self, name: ModelSlotName) -> ModelBinding:
        try:
            return self.slots[name]
        except KeyError as exc:
            raise ConfigLoadError(f"未知模型档位: {name}") from exc


class RouterThresholds(BaseModel):
    primary_high: float = Field(ge=0.0, le=1.0)
    primary_low: float = Field(ge=0.0, le=1.0)
    secondary: float = Field(ge=0.0, le=1.0)
    ambiguity_margin: float = Field(gt=0.0, le=1.0)
    tentative_slot: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_order(self) -> RouterThresholds:
        if self.primary_low >= self.primary_high:
            raise ValueError("primary_low 必须小于 primary_high")
        if not self.primary_low <= self.secondary <= self.primary_high:
            raise ValueError("secondary 必须位于 primary_low 与 primary_high 之间")
        return self


class RecallPolicy(BaseModel):
    top_k: int = Field(ge=1, le=20)
    novelty_floor: float = Field(ge=0.0, le=1.0)
    user_memory_first: Literal[True]
    project_memory_after_intent: Literal[True]


class ClarificationPolicy(BaseModel):
    max_rounds: int = Field(ge=1, le=5)
    one_question_only: Literal[True]
    high_risk_soft_confirm: Literal[True]


class AutoCalibratePolicy(BaseModel):
    enabled: bool
    schedule: Literal["weekly"]
    min_samples: int = Field(ge=50)
    target_direct_error_rate: float = Field(gt=0.0, lt=0.1)
    target_clarify_rate: float = Field(gt=0.0, lt=0.5)
    primary_high_min: float = Field(ge=0.0, le=1.0)
    primary_high_max: float = Field(ge=0.0, le=1.0)
    primary_low_min: float = Field(ge=0.0, le=1.0)
    primary_low_max: float = Field(ge=0.0, le=1.0)
    tighten_only: Literal[True]

    @model_validator(mode="after")
    def validate_clamps(self) -> AutoCalibratePolicy:
        if self.primary_high_min >= self.primary_high_max:
            raise ValueError("primary_high_min 必须小于 primary_high_max")
        if self.primary_low_min >= self.primary_low_max:
            raise ValueError("primary_low_min 必须小于 primary_low_max")
        return self


class ExecutionPolicy(BaseModel):
    max_plan_revisions: int = Field(ge=0, le=5)
    max_tasks: int = Field(ge=1, le=100)
    task_timeout_seconds: int = Field(ge=10, le=3600)
    tool_retry_attempts: int = Field(ge=1, le=5)
    serial_task_dag: Literal[True]


class RouterConfig(BaseModel):
    version: Literal[1]
    thresholds: RouterThresholds
    recall: RecallPolicy
    clarification: ClarificationPolicy
    auto_calibrate: AutoCalibratePolicy
    execution: ExecutionPolicy


class TierQuota(BaseModel):
    token_budget_daily: int = Field(ge=1)
    project_token_budget_daily: int = Field(ge=1)
    rpm: int = Field(ge=1, le=10000)
    max_concurrent_sessions: int = Field(ge=1, le=100)
    session_token_budget: int = Field(ge=1)


class QuotaConfig(BaseModel):
    version: Literal[1]
    warning_ratio: float = Field(gt=0.0, lt=1.0)
    redis_prefix: Literal["ai:ratelimit"]
    session_key_prefix: Literal["ai:session"]
    tiers: dict[str, TierQuota]

    @model_validator(mode="after")
    def validate_tiers(self) -> QuotaConfig:
        if set(self.tiers) != {"free", "pro", "max"}:
            raise ValueError("quota.yaml 必须精确声明 free/pro/max 三档")
        return self

    def tier(self, name: TierName) -> TierQuota:
        try:
            return self.tiers[name]
        except KeyError as exc:
            raise ConfigLoadError(f"未知配额档位: {name}") from exc


class RuntimeConfig(BaseModel):
    models: ModelsConfig
    router: RouterConfig
    quota: QuotaConfig

    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR) -> RuntimeConfig:
        return cls(
            models=_load_yaml_model(config_dir / "models.yaml", ModelsConfig),
            router=_load_yaml_model(config_dir / "router.yaml", RouterConfig),
            quota=_load_yaml_model(config_dir / "quota.yaml", QuotaConfig),
        )


def _load_yaml_model[T: BaseModel](path: Path, model_type: type[T]) -> T:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigLoadError(f"配置文件不存在: {path}") from exc
    except OSError as exc:
        raise ConfigLoadError(f"配置文件读取失败: {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"配置文件 YAML 语法错误: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"配置文件根节点必须是对象: {path}")
    try:
        return model_type.model_validate(raw)
    except Exception as exc:
        raise ConfigLoadError(f"配置契约校验失败: {path}: {exc}") from exc


runtime_config = RuntimeConfig.load()


class Settings(BaseSettings):
    """部署环境配置；密钥只允许来自环境变量或项目根目录 .env。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_title: str = "SeedAI API"
    env: Literal["local", "dev", "test", "production"] = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 7101
    business_api_port: int = 7101
    ai_service_port: int = 7102
    ai_servers: str = ""
    ai_server_health_path: str = "/healthz"
    ai_service_url: str = "http://localhost:7101"
    business_service_url: str = "http://localhost:7101"

    redis_url: str = "redis://redis:6379/0"
    database_url: str = "sqlite+aiosqlite:///./seedai.db"
    mysql_url: str = ""
    chroma_url: str = "http://chroma:8000"
    chroma_api_version: Literal["v2"] = "v2"
    dev_memory_queue: bool = False

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_ttl: int = Field(default=3600, ge=300)
    refresh_token_ttl: int = Field(default=7 * 24 * 3600, ge=3600)
    seed_super_admin: str = ""
    provider_encryption_key: str = ""
    provider_encryption_key_prev: str = ""
    # KEK 版本号：换 KEK 时同步 +1，rotate 据此判断密文是否需要升级重加密。
    provider_encryption_key_version: int = 1
    critical_admin_allowlist: str = ""

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.7-plus"
    qwen_embedding_key: str = ""
    qwen_embedding_model: Literal["text-embedding-v3"] = "text-embedding-v3"
    qwen_embedding_base_url: str = "https://ws-rao72of9tmiy6llq.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    hy3_api_key: str = ""
    hy3_api_key_demo: str = ""
    hy3_base_url: str = "https://tokenhub.tencentmaas.com/v1"
    hy3_model: str = "hy3"
    default_model: Literal["standard", "pro", "ultra"] = "standard"

    chat_recent_redis_ttl: int = Field(default=1800, ge=60)
    chat_recent_limit: int = Field(default=10, ge=1, le=100)
    # S4 BoundedPlan 运行期意图数量软上限（数据模型硬上限见 contracts.MAX_ACTION_ITEMS）。
    # 改此处即可调整单轮最多拆解多少意图/动作；配置值超过硬上限会在导入阶段 fail-fast。
    max_action_items: int = Field(default=MAX_ACTION_ITEMS, ge=1, le=MAX_ACTION_ITEMS)
    conversation_summary_ttl: int = Field(default=1800, ge=60)
    cache_user_ttl: int = Field(default=9000, ge=60)
    worker_pool_size: int = Field(default=4, ge=1, le=128)
    worker_concurrency: int = Field(default=2, ge=1, le=64)
    fallback_order: str = "hy3,qwen,deepseek"
    stream_event_ttl: int = Field(default=3600, ge=120)
    trace_replay_after_max: int = Field(default=100000, ge=100)
    ai_connect_token_ttl: int = Field(default=300, ge=60)

    free_daily_quota: int = Field(default=50, ge=1)
    plan_daily_quota: dict[str, int] = Field(
        default_factory=lambda: {"free": 50, "pro": 500, "enterprise": 5000}
    )

    site_domain: str = "seedai.huzhen.net.cn"
    preview_domain: str = "seedhtml.huzhen.net.cn"
    # REQ-PREVIEW-001: 预览必须落在「不携带平台凭证」的独立 Origin。
    # preview_base_url 为空时: production 由 preview_domain 推导 https origin;
    # 本地开发降级为同源相对路径(签名与 CSP 仍然生效, 只是缺少 Origin 物理隔离)。
    preview_base_url: str = ""
    preview_grant_ttl: int = Field(default=600, ge=60, le=3600)
    cookie_domain: str = ""
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:7100,http://127.0.0.1:7100,http://seedai.huzhen.net.cn:7100,http://seedai.huzhen.net.cn,https://seedai.huzhen.net.cn"

    chroma_collection_components: str = "components"
    chroma_collection_memory: str = "memory"
    chroma_collection_cache: str = "cache_generate"
    chroma_collection_user_preferences: str = "user_preferences"
    chroma_collection_project_memory: str = "project_memory"
    chroma_collection_project_code: str = "project_code"
    chroma_collection_conversation_context: str = "conversation_context"
    chroma_collection_error_patterns: str = "error_patterns"
    chroma_collection_intents: str = "intents"
    chroma_collection_kb_design: str = "kb_design"
    chroma_collection_rag_corpus: str = "rag_corpus"
    rag_top_k: int = Field(default=5, ge=1, le=20)

    split_b_enabled: bool = True
    split_b_max_subtasks: int = Field(default=6, ge=1, le=20)
    split_escalate_low_conf: float = Field(default=0.6, ge=0.0, le=1.0)
    split_repair_max_rounds: int = Field(default=2, ge=0, le=5)
    qc_judges: str = "deepseek,qwen,hy3"
    qc_solo_needs_review_overall: float = Field(default=7.0, ge=0.0, le=10.0)
    qc_needs_review_variance: float = Field(default=4.0, ge=0.0)
    qc_timeout_seconds: float = Field(default=60.0, gt=0.0)
    qc_fix_enabled: bool = True
    qc_fix_max_rounds: int = Field(default=2, ge=0, le=5)

    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = "seedhtml-1252059540"
    cos_region: str = "ap-guangzhou"
    cos_preview_domain: str = "https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com"
    cos_base_path: str = "previews"
    cos_ttl_days: int = Field(default=0, ge=0)
    artifact_dir: str = "./artifacts"

    tavily_api_key: str = ""
    serper_api_key: str = ""
    web_search_top_k: int = Field(default=5, ge=1, le=20)
    image_api_key: str = ""
    image_api_base: str = ""
    image_model: str = "dall-e-3"

    @model_validator(mode="after")
    def normalize_and_validate(self) -> Settings:
        artifact_path = Path(self.artifact_dir)
        if not artifact_path.is_absolute():
            artifact_path = (PROJECT_ROOT / artifact_path).resolve()
        self.artifact_dir = str(artifact_path)

        if self.database_url.startswith("sqlite") and self.mysql_url:
            self.database_url = self.mysql_url
        if self.database_url.startswith("mysql+pymysql://"):
            self.database_url = "mysql+aiomysql://" + self.database_url[len("mysql+pymysql://"):]
        elif self.database_url.startswith("mysql://"):
            self.database_url = "mysql+aiomysql://" + self.database_url[len("mysql://"):]
        if "mysql" in self.database_url and "charset=" not in self.database_url:
            separator = "&" if "?" in self.database_url else "?"
            self.database_url += f"{separator}charset=utf8mb4"

        if self.env == "production":
            self._validate_production()
        return self

    def _validate_production(self) -> None:
        errors: list[str] = []
        if self.jwt_secret == "dev-secret-change-me" or len(self.jwt_secret) < 32:
            errors.append("JWT_SECRET 必须替换为至少 32 字符的生产密钥")
        if not self.database_url.startswith("mysql+aiomysql://"):
            errors.append("生产环境 DATABASE_URL 必须使用 mysql+aiomysql")
        if not self.redis_url.startswith(("redis://", "rediss://")):
            errors.append("生产环境 REDIS_URL 必须是 redis:// 或 rediss://")
        if not self.chroma_url.startswith(("http://", "https://")):
            errors.append("生产环境 CHROMA_URL 必须是 HTTP(S) 地址")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.provider_encryption_key):
            errors.append("PROVIDER_ENCRYPTION_KEY 必须是 64 位十六进制 AES-256 密钥")
        if not self.qwen_api_key:
            errors.append("QWEN_API_KEY 缺失，S2/S4/S8 无法运行")
        if not self.qwen_embedding_key:
            errors.append("QWEN_EMBEDDING_KEY 缺失，L2 向量召回无法运行")
        if not (self.hy3_api_key or self.hy3_api_key_demo):
            errors.append("HY3_API_KEY 缺失，exec_standard 无法运行")
        if not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY 缺失，exec_ultra 无法运行")
        if not self.cos_secret_id or not self.cos_secret_key:
            errors.append("COS_SECRET_ID/COS_SECRET_KEY 缺失，site_deploy 无法运行")
        if "*" in self.cors_origin_list:
            errors.append("生产 CORS_ORIGINS 禁止使用通配符 *")
        # REQ-PREVIEW-001: 预览 Origin 必须独立于平台 Origin, 否则 iframe 内产物可读平台 Cookie。
        preview_origin = self.preview_origin
        if not preview_origin:
            errors.append("生产 PREVIEW_BASE_URL/PREVIEW_DOMAIN 缺失，预览无法落在独立 Origin")
        elif preview_origin in self.cors_origin_list:
            errors.append(f"生产预览 Origin 不得与平台 Origin 重合: {preview_origin}")
        if errors:
            raise ValueError("生产配置校验失败: " + "; ".join(errors))

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_dev(self) -> bool:
        return not self.is_production

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def preview_origin(self) -> str:
        """预览产物的对外 Origin(无尾斜杠); 空串表示同源相对路径(仅本地开发)。"""
        if self.preview_base_url.strip():
            return self.preview_base_url.strip().rstrip("/")
        if self.is_production and self.preview_domain.strip():
            return f"https://{self.preview_domain.strip().rstrip('/')}"
        return ""

    @property
    def intent_super_fast(self) -> float:
        return runtime_config.router.thresholds.primary_high

    @property
    def intent_novelty(self) -> float:
        return runtime_config.router.recall.novelty_floor

    @property
    def intent_commit(self) -> float:
        return runtime_config.router.thresholds.primary_high

    @property
    def intent_clarify_lo(self) -> float:
        return runtime_config.router.thresholds.primary_low

    @property
    def intent_clarify_max_rounds(self) -> int:
        return runtime_config.router.clarification.max_rounds

    @property
    def intent_top_k(self) -> int:
        return runtime_config.router.recall.top_k

    def model_binding(self, slot: ModelSlotName) -> ModelBinding:
        return runtime_config.models.slot(slot)

    def model_base_url(self, slot: ModelSlotName) -> str:
        binding = self.model_binding(slot)
        override = os.getenv(binding.base_url_env, "").strip()
        return override or str(binding.default_base_url).rstrip("/")

    def provider_api_key(self, provider: ProviderName) -> str:
        mapping = {
            "qwen": self.qwen_api_key,
            "hy3": self.hy3_api_key or self.hy3_api_key_demo,
            "deepseek": self.deepseek_api_key,
        }
        return mapping[provider]


settings = Settings()

__all__ = [
    "BACKEND_ROOT",
    "CONFIG_DIR",
    "ENV_FILE",
    "PROJECT_ROOT",
    "ConfigLoadError",
    "EmbeddingBinding",
    "ModelBinding",
    "ModelSlotName",
    "ModelsConfig",
    "QuotaConfig",
    "RouterConfig",
    "RuntimeConfig",
    "Settings",
    "runtime_config",
    "settings",
]
