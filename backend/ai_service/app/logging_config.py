"""日志配置:本地按日期落盘 + 控制台双写。

背景
----
此前后端日志只走 uvicorn 默认 stdout,容器/进程一重启就丢失,且无法按天回溯。
按需求,这里把日志同时写到:
  1. 控制台(stdout)——保持原有体验,docker 场景仍可由容器收集;
  2. 本地文件 `backend/ai_service/logs/<service>.log`——按**自然日**滚动切分,
     过期文件自动命名为 `<service>.log.YYYY-MM-DD`(由 TimedRotatingFileHandler
     在每天午夜切换时重命名),保留最近 30 天。

覆盖范围
------
- 应用内所有 `logging.getLogger(...)` 日志(如 ai_service.queue / ai_service.router);
- uvicorn 自身的访问日志(uvicorn.access)与错误日志(uvicorn.error),一并落盘。

幂等性
------
`setup_logging()` 通过检测根 logger 是否已挂载同路径的日期滚动 Handler 来避免
uvicorn --reload 重复 import 时 handler 叠加导致日志重复输出。
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 日志目录固定放在本服务包的上一级(backend/ai_service/logs),
# 不受进程启动 CWD 影响,保证本地直跑与 docker 内路径一致。
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_logging(service_name: str, *, level: int = logging.INFO) -> None:
    """初始化日志:控制台 + 按日期滚动的本地文件,双写。

    Args:
        service_name: 服务名,决定文件名前缀(如 "ai_service" -> ai_service.log)。
        level: 最低记录级别,默认 INFO(DEBUG 在排查时可临时调低)。
    """
    log_path = _LOG_DIR / f"{service_name}.log"

    root = logging.getLogger()
    # 防止 reload/重复调用导致 handler 叠加:若已挂同路径的滚动 Handler 则跳过。
    for h in root.handlers:
        if isinstance(h, TimedRotatingFileHandler) and str(log_path) in getattr(
            h, "baseFilename", ""
        ):
            return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 按自然日滚动:每天午夜把当前 ai_service.log 重命名为 ai_service.log.YYYY-MM-DD,
    # 新文件继续写 ai_service.log;backupCount=30 仅保留最近 30 天,避免无限膨胀。
    file_handler = TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # 让 uvicorn 的访问/错误日志也走同一套 handler(关闭向上传播避免重复)。
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.addHandler(file_handler)
        lg.addHandler(console_handler)
        lg.propagate = False
