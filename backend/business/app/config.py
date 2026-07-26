"""业务服务配置。从项目根 .env 读取(密钥只在此出现,不进文档/记忆)。"""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 用绝对路径定位项目根目录的 .env,不受启动目录影响(与 AI 服务一致)。
# 之前相对 env_file=".env" 在 backend/business/app 下启动会找不到根 .env,
# 导致 DATABASE_URL/MYSQL_URL/JWT 等回落默认值(如 SQLite),与 docker 行为不一致。
# parents[3] 的由来:本文件位于 <仓库根>/backend/business/app/config.py,
# 向上 3 级(__file__ -> app -> business -> backend -> 仓库根)即拿到仓库根,
# 从而稳定读到仓库根的 .env(无论进程从哪个目录启动)。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), extra="ignore")

    # 服务间通信:业务服务唯一对外,AI 服务仅内网
    ai_service_url: str = "http://ai-service:7102"

    # 业务服务自身监听端口(本地直跑 uvicorn 默认 8000,此处显式锁定 7101;
    # docker-compose 也会注入 BUSINESS_API_PORT=7101 覆盖;main.py __main__ 块使用)
    business_api_port: int = 7101

    # 数据层
    redis_url: str = "redis://redis:6379/0"
    # 主 DB URL(MySQL/SQLite 均可);docker-compose 注入 mysql+aiomysql。
    # 本地若未显式给 DATABASE_URL 但给了 MYSQL_URL,则回退采用(见 model_post_init)。
    database_url: str = "sqlite+aiosqlite:///./seedai.db"
    mysql_url: str = ""

    def model_post_init(self, __context) -> None:
        # 兼容旧键 MYSQL_URL:仅在仍取 SQLite 默认值且提供了 MYSQL_URL 时切换
        if self.database_url.startswith("sqlite") and self.mysql_url:
            self.database_url = self.mysql_url
        # 业务端使用异步 SQLAlchemy,MySQL 必须是异步驱动(mysql+aiomysql)。
        # .env 里常写同步驱动(mysql+pymysql)或裸 mysql://,这里自动升级为异步,
        # 保持与 docker-compose(DATABASE_URL=mysql+aiomysql)一致 —— 本地直跑也能连同一套 MySQL,
        # 不改 .env 的库地址/账号(那套是用户统一的)。
        u = self.database_url
        if u.startswith("mysql+pymysql://"):
            u = "mysql+aiomysql://" + u[len("mysql+pymysql://") :]
        elif u.startswith("mysql://"):
            u = "mysql+aiomysql://" + u[len("mysql://") :]
        self.database_url = u
        # 强制 utf8mb4(支持 emoji), MySQL 默认 utf8=3 字节不够
        if "mysql" in self.database_url and "charset=" not in self.database_url:
            sep = "&" if "?" in self.database_url else "?"
            self.database_url += f"{sep}charset=utf8mb4"

    # JWT
    # ⚠️ 默认 jwt_secret 仅用于本地开发,生产必须在 .env 覆盖为强随机值,
    # 否则任何人可用该已知密钥伪造 token。HS256 为对称签名,签发与校验共用此密钥。
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 7 * 24 * 3600

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        import os, sys
        if self.jwt_secret == "dev-secret-change-me" and os.environ.get("ENV", "") not in ("dev", "local", ""):
            sys.stderr.write("FATAL: JWT_SECRET 仍为默认值,生产环境拒绝启动。请在 .env 中覆盖为随机强值。\n")
            sys.exit(1)
        return self

    # 超级管理员种子(文档 §2.3):首次启动把该用户名的角色置为 super_admin。
    # 避免角色自举漏洞 —— 不允许自助注册成 super_admin,只能由种子/已有 super_admin 赋予。
    seed_super_admin: str = ""

    # 缓存默认 TTL
    cache_user_ttl: int = 9000  # 150 min（×5）

    # ───────────────────────────────────────────────────────────
    # 对话/上下文相关配置（集中管理，避免 proxy.py 内散落魔法数字）
    # ───────────────────────────────────────────────────────────
    # 最近对话消息上下文的 Redis 滑动窗口 TTL（秒）。
    # 原先硬编码 600s(10min)，现统一为 30min：用户两次对话间隔较长时，
    # 仍命中缓存、不回源 MySQL，避免 LIMIT 截断导致的上下文塌缩。
    chat_recent_redis_ttl: int = 1800

    # 组装给 AI 核心的「最近 N 条消息」条数上限（原代码硬编码 limit(5)）。
    # 提到 10 条，保留更早的建站/生成意图，配合 TTL 放大，缓解上下文丢失。
    chat_recent_limit: int = 10

    # 对话摘要（summary:* / summary_cnt:*）的 Redis TTL（秒）。
    # 原先硬编码 86400(1d)，现统一为 30min，与对话上下文窗口对齐；
    # 1d 过久会让陈旧摘要长期污染后续意图判断，且无 MySQL 兜底回写，
    # 过期后反而丢失。30min 滑动窗口更贴近「最近会话语义」。
    conversation_summary_ttl: int = 1800

    # 分布式 AI 服务发现：业务端 /api/chat 不直连单一 AI 核心，而是返回一个
    # 经过健康/负载筛选的 AI 服务地址，由前端 EventSource 直连（省一层 SSE 转发）。
    # 格式：逗号分隔的 base URL 列表；为空时回退到单实例 ai_service_url。
    ai_servers: str = ""
    # AI 服务健康探测路径（用于 /api/chat 选址时探活）
    ai_server_health_path: str = "/healthz"

    # 配额限流(①-b):free 套餐默认 50 次/天;可扩展各套餐每日上限。
    # 全局令牌桶(方案 a)本期未做,仅做基于 user_id 的每日配额。
    free_daily_quota: int = 50
    plan_daily_quota: dict = {"free": 50, "pro": 500, "enterprise": 5000}

    # 站点域名(§4-C:主站 + 隔离预览子域)
    site_domain: str = "seedai.huzhen.net.cn"
    preview_domain: str = "seedhtml.huzhen.net.cn"
    # 鉴权 Cookie 域(空=按宿主;生产设为 .huzhen.net.cn 或主站域,预览子域不持有)
    cookie_domain: str = ""
    # Cookie 是否仅 HTTPS(生产 true;本地 http 开发设 false 才能写入)
    cookie_secure: bool = False

    # CORS(前端 origin,逗号分隔;默认含本地与站点域)
    # 含 bare 域名与 https 变体:bare 域名场景(如 nginx 反代到 80/443)下
    # 若前端改用绝对地址跨域调用 seedapi,仍可带 Cookie 通行。
    cors_origins: str = "http://localhost:7100,http://seedai.huzhen.net.cn:7100,http://seedai.huzhen.net.cn,https://seedai.huzhen.net.cn"


settings = Settings()
