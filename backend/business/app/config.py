"""业务服务配置。从 .env 读取(密钥只在此出现,不进文档/记忆)。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 服务间通信:业务服务唯一对外,AI 服务仅内网
    ai_service_url: str = "http://ai-service:7102"

    # 数据层
    redis_url: str = "redis://redis:6379/0"
    # M0 默认用 SQLite 便于本地直跑;docker-compose 注入 MySQL(aiomysql)
    database_url: str = "sqlite+aiosqlite:///./seedai.db"

    # JWT
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl: int = 1800          # 30 min
    refresh_token_ttl: int = 7 * 24 * 3600  # 7 days

    # 缓存默认 TTL
    cache_user_ttl: int = 1800            # 30 min

    # 站点域名(§4-C:主站 + 隔离预览子域)
    site_domain: str = "seedai.huzhen.net.cn"
    preview_domain: str = "seedhtml.huzhen.net.cn"
    # 鉴权 Cookie 域(空=按宿主;生产设为 .huzhen.net.cn 或主站域,预览子域不持有)
    cookie_domain: str = ""

    # CORS(前端 origin,逗号分隔;默认含本地与站点域)
    cors_origins: str = "http://localhost:7100,http://seedai.huzhen.net.cn:7100"


settings = Settings()
