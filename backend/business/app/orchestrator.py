"""控制面 Orchestrator(⑥-b):真实调用本地 docker compose 执行启停/扩缩容,并返回执行日志。

安全约束:
  - 仅由 `require_super_admin` 守卫的端点调用(admin.py)。
  - 命令参数由服务端白名单(service 名 + 数字 replicas)构造,**不拼接用户任意 shell 字符串**,
    避免命令注入;`asyncio.create_subprocess_exec` 直接传参列表(不经 shell)。
  - 受控服务限定在 ALLOWED_SERVICES,防止对任意服务名执行 docker 操作。
部署形态当前是 docker-compose(见 docker-compose.yml);若日后切 K8s,只需改这里。
"""

import asyncio
import logging
import shlex

logger = logging.getLogger("business.orchestrator")

# 受控服务白名单(防止对任意服务名执行 docker 操作)
ALLOWED_SERVICES = {"business", "ai-service", "frontend", "mysql", "redis", "chroma"}


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
    """扩缩容:docker compose up -d --scale <service>=<replicas>。"""
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"unknown service: {service}"}
    if not (1 <= replicas <= 20):
        return {"ok": False, "error": "replicas must be 1..20"}
    cmd = _compose_base() + ["up", "-d", "--scale", service, str(replicas)]
    return await _exec(cmd)


async def run_stop(service: str) -> dict:
    """停止:docker compose stop <service>。"""
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"unknown service: {service}"}
    cmd = _compose_base() + ["stop", service]
    return await _exec(cmd)


async def run_start(service: str) -> dict:
    """启动:docker compose start <service>(⑥-b 补充控制面能力)。"""
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"unknown service: {service}"}
    cmd = _compose_base() + ["start", service]
    return await _exec(cmd)
