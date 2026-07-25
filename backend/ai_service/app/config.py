"""AI 服务配置(仅内网)。从项目根 .env 加载(extra="ignore" 容忍未声明变量)。"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# 用绝对路径定位项目根目录的 .env,不受启动目录影响。
# 之前用相对 env_file=".env",本地从 backend/ai_service/app 启动时找不到根 .env,
# 导致模型 Key 全为空 → hy3 调用腾讯 TokenHub 401 鉴权失败。
# docker 内该绝对路径不存在时 pydantic-settings 会静默忽略,回退到 compose 注入的环境变量,安全。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), extra="ignore")

    # 模型 Key(仅 AI 服务持有)
    deepseek_api_key: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = (
        "https://ws-rao72of9tmiy6llq.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    qwen_model: str = "qwen-plus"
    hy3_api_key: str = ""
    hy3_api_key_demo: str = ""
    hy3_base_url: str = "https://tokenhub.tencentmaas.com/v1"
    hy3_model: str = "hy3"

    # agent_delete 回调业务服务
    business_service_url: str = "http://business:7101"

    # 服务
    ai_service_port: int = 7102
    # 默认降级序(2-C / #31):HY3 → Qwen → DeepSeek,用户请求可覆盖
    fallback_order: str = "hy3,qwen,deepseek"

    # Worker 并发数(1-C)
    worker_concurrency: int = 2

    # 数据层
    redis_url: str = "redis://redis:6379/0"
    chroma_url: str = "http://chroma:8000"
    # 本地开发用内存队列(进程内,不依赖 Redis),生产环境应设为 false 走 Redis Stream
    dev_memory_queue: bool = False

    # 向量检索 / 记忆(Chroma + Qwen text-embedding,§7)
    qwen_embedding_key: str = ""  # DashScope embedding key(可复用 Qwen 大模型 key)
    qwen_embedding_model: str = "text-embedding-v3"
    chroma_collection_components: str = "components"
    chroma_collection_memory: str = "memory"
    chroma_collection_cache: str = "cache_gen"
    # v0.9.0 六集合扩展(P0)
    chroma_collection_user_preferences: str = "user_preferences"
    chroma_collection_project_memory: str = "project_memory"
    chroma_collection_project_code: str = "project_code"
    chroma_collection_error_patterns: str = "error_patterns"
    # v1.2.0 混合级联意图识别: 意图向量索引集合
    chroma_collection_intents: str = "intents"
    rag_top_k: int = 5

    # ── 意图识别阈值(混合级联, 集中可调, 单一来源) ──
    intent_super_fast: float = 0.90        # 强规则 + 向量 top1 相似度 ≥ 此值 → 跳过 LLM
    intent_novelty: float = 0.45           # top5 最高相似度 < 此值 且无规则命中 → 闲聊兜底
    intent_commit: float = 0.80            # 置信度 ≥ 此值 → 直接路由
    intent_clarify_lo: float = 0.45        # 低于此值进入澄清/兜底判定
    intent_clarify_max_rounds: int = 2     # 最多追问轮次(≤2)

    # ── 后置质检(QC)配置(单一来源) ──
    qc_judges: str = "deepseek,qwen,hy3"    # 三裁判模型(逗号分隔, 顺序即 scores 下标)
    qc_needs_review_variance: float = 4.0   # 任一维方差 ≥ 此值 → needs_review(分歧大)
    qc_timeout_seconds: float = 60.0        # 单次 QC 调用超时(由调用方 asyncio.wait_for 控制)
    # ── 质量闭环(needs_review → agent_review → 重打分) ──
    qc_fix_enabled: bool = True             # 后置 QC 标记 needs_review 时, 自动触发 agent_review 修复并重打分
    qc_fix_max_rounds: int = 2              # 修复循环上限(防无限); 每轮: agent_review 出 fixed_code → 重写盘+COS → 重跑 QC

    # 对象存储(COS 预览投递,§10 / §5.9 tool:cos_upload)
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = "seedhtml-1252059540"
    cos_region: str = "ap-guangzhou"
    cos_preview_domain: str = "https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com"
    cos_base_path: str = "previews"
    cos_ttl_days: int = 0

    # 检索 / 搜索工具(§5.9 tool:web_search)
    tavily_api_key: str = ""
    serper_api_key: str = ""
    web_search_top_k: int = 5

    # 本地产物目录(tool:file_write 落盘,随后推 COS)
    artifact_dir: str = "./artifacts"

    # 图像生成(可选;未配置时 image_generate 返回清晰状态,§5.9 tool:image_generate)
    image_api_key: str = ""
    image_api_base: str = ""
    image_model: str = "dall-e-3"


settings = Settings()
