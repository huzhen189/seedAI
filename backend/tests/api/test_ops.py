"""运维端点测试（M10b）：路由注册与鉴权门禁，不触发全量 init_db。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ops import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_ops_status_requires_auth() -> None:
    # /ops/status 必须 require_admin，未带 token 返回 401。
    c = _client()
    assert c.get("/ops/status").status_code == 401


def test_readyz_reachable() -> None:
    # /readyz 始终可达（依赖异常时返回 503 而非 5xx）。
    c = _client()
    assert c.get("/readyz").status_code in (200, 503)


def test_metrics_exposition_format() -> None:
    # /metrics 返回 Prometheus 文本格式，含存活/依赖指标（依赖探测失败仅置 0，不影响格式）。
    c = _client()
    resp = c.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "seedai_up" in body
    assert "seedai_dependency_up" in body
    # 若存在熔断器实例则应有对应指标行（全局注册表非空时）。
    if "seedai_circuit_breaker_state" in body:
        assert "seedai_circuit_breaker_failures" in body
