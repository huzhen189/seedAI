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

    # 服务
    ai_service_port: int = 7102
    # 默认降级序(2-C / #31):HY3 → Qwen → DeepSeek,用户请求可覆盖
    fallback_order: str = "hy3,qwen,deepseek"

    # Worker 并发数(1-C)
    worker_concurrency: int = 2

    # 数据层
    redis_url: str = "redis://redis:6379/0"
    chroma_url: str = "http://chroma:8000"

    # 向量检索 / 记忆(Chroma + Qwen text-embedding,§7)
    qwen_embedding_key: str = ""  # DashScope embedding key(可复用 Qwen 大模型 key)
    qwen_embedding_model: str = "text-embedding-v3"
    chroma_collection_components: str = "components"
    chroma_collection_memory: str = "memory"
    chroma_collection_cache: str = "cache_gen"
    rag_top_k: int = 5

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
