"""控制面部署编排(⑥-b):根据运行环境(env)选择底层实现执行启停/扩缩容,并返回执行日志。

两种部署形态:
  - dev / local(docker-compose 形态): 调用 `docker compose`(up -d --scale / stop / start)。
  - production(正式服务器, 裸跑 systemd 形态): 调用 `systemctl`(stop/start seedai-backend)。
    production 下单进程无副本概念, scale 不再走 docker --scale, 改为提示/降级。

安全约束:
  - 仅由 `require_super_admin` 守卫的端点调用(admin.py)。
  - 命令参数由服务端白名单(service 名 + 数字 replicas)构造,**不拼接用户任意 shell 字符串**,
    避免命令注入;`asyncio.create_subprocess_exec` 直接传参列表(不经 shell)。
  - 受控服务限定在 ALLOWED_SERVICES,防止对任意服务名执行操作。
"""

import asyncio
import logging
import shlex

from app.config import settings


logger = logging.getLogger("business.deployment")

# 受控服务白名单(防止对任意服务名执行操作)
ALLOWED_SERVICES = {"business", "ai-service", "frontend", "mysql", "redis", "chroma"}

# 正式服务器裸跑时, 业务进程对应的 systemd 单元名
SYSTEMD_BACKEND_UNIT = "seedai-backend"


def _compose_base() -> list[str]:
    """docker compose 命令前缀(优先插件式 `docker compose`,回退 `docker-compose`)。"""
    return ["docker", "compose"]


async def _exec(cmd: list[str], timeout: float = 30.0) -> dict:
    """执行一条命令并收集输出;返回结构化结果(含执行日志,截断防超长)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode("utf-8", "ignore") if out else ""
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "command": " ".join(shlex.quote(c) for c in cmd),
            "log": text[-2000:],  # 截断,避免回传过长日志
        }
    except asyncio.TimeoutError:
        return {"ok": False, "command": " ".join(cmd), "error": f"timeout after {timeout}s"}
    except Exception as e:  # 命令不存在/权限不足等,记录并降级返回,不抛出
        logger.warning("orchestrator exec failed: %s", e)
        return {"ok": False, "command": " ".join(cmd), "error": str(e)}


async def run_scale(service: str, replicas: int) -> dict:
    """扩缩容。

    - dev 形态: docker compose up -d --scale <service>=<replicas>。
    - production 形态: 单进程 systemd 运行, 无 docker 副本概念, 返回明确提示(不报错假装成功)。
    """
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"unknown service: {service}"}
    if not (1 <= replicas <= 20):
        return {"ok": False, "error": "replicas must be 1..20"}
    if settings.is_production:
        return {
            "ok": False,
            "command": "systemctl (skipped)",
            "error": (
                "production 形态为单进程 systemd 运行, 不支持 docker 式副本扩容。"
                "如需扩容请水平扩展多实例并前置负载, 或调整后端资源配置。"
            ),
        }
    cmd = _compose_base() + ["up", "-d", "--scale", service, str(replicas)]
    return await _exec(cmd)


async def run_stop(service: str) -> dict:
    """停止。

    - dev 形态: docker compose stop <service>。
    - production 形态: systemctl stop seedai-backend(忽略 service 白名单中的其他名, 统一停后端单元)。
    """
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"unknown service: {service}"}
    if settings.is_production:
        return await _exec(["systemctl", "stop", SYSTEMD_BACKEND_UNIT])
    cmd = _compose_base() + ["stop", service]
    return await _exec(cmd)


async def run_start(service: str) -> dict:
    """启动。

    - dev 形态: docker compose start <service>。
    - production 形态: systemctl start seedai-backend。
    """
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"unknown service: {service}"}
    if settings.is_production:
        return await _exec(["systemctl", "start", SYSTEMD_BACKEND_UNIT])
    cmd = _compose_base() + ["start", service]
    return await _exec(cmd)

