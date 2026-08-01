"""研究/检索类原子工具（规范 §9.2 + §8.3）。

web_search / web_fetch / browser_capture / rag_query 当前**无后端联网或 Chroma 检索服务**
可用：这些 Tool 的契约（风险、沙箱、出口、reconcile）已登记，但运行时在无对应
后端时**明确返回 failed**，绝不静默成功或伪造结果（§9.2：Tool 必须返回 ToolResult）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import Domain, ErrorEnvelope, RiskLevel
from app.tools._registry import ToolMeta
from app.tools.base import BaseTool, ToolContext, ToolResult

_UNAVAILABLE = "backend_unavailable"


class WebSearchTool(BaseTool):
    meta = ToolMeta(
        tool_id="web_search",
        risk=RiskLevel.LOW,
        domain=Domain.RESEARCH,
        description="搜索并返回来源元数据。", egress_profile="search_api",
        sandbox_profile="egress_limited", idempotency=False, reconcile_strategy="none",
        factory=lambda: WebSearchTool(),
    )

    async def run(self, ctx: ToolContext, *, query: str) -> ToolResult:
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="research",
            what="web_search 暂未接入搜索后端", why="无 search provider 配置",
            next="待接入搜索服务后可用", retryable=False, retry_scope="none"))


class WebFetchTool(BaseTool):
    meta = ToolMeta(
        tool_id="web_fetch",
        risk=RiskLevel.LOW,
        domain=Domain.RESEARCH,
        description="受限抓取、大小/域名/超时控制（SSRF/DNS rebinding 防护）。",
        egress_profile="fetch_http", sandbox_profile="egress_limited", idempotency=False,
        reconcile_strategy="none", factory=lambda: WebFetchTool(),
    )

    async def run(self, ctx: ToolContext, *, url: str) -> ToolResult:
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="research",
            what="web_fetch 暂未接入抓取后端", why="无 fetch 客户端配置",
            next="待接入受限抓取后可用", retryable=False, retry_scope="none"))


class RagQueryTool(BaseTool):
    meta = ToolMeta(
        tool_id="rag_query",
        risk=RiskLevel.LOW,
        domain=Domain.RESEARCH,
        description="Chroma scope 隔离检索。", egress_profile="vector_db",
        sandbox_profile="read_only", idempotency=False, reconcile_strategy="none",
        factory=lambda: RagQueryTool(),
    )

    async def run(self, ctx: ToolContext, *, collection: str, query: str,
                  session: AsyncSession | None = None) -> ToolResult:
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="memory",
            what="rag_query 暂无可检索的向量集合", why="memory 层未初始化",
            next="待知识库就绪后可用", retryable=False, retry_scope="none"))


class BrowserCaptureTool(BaseTool):
    meta = ToolMeta(
        tool_id="browser_capture",
        risk=RiskLevel.LOW,
        domain=Domain.RESEARCH,
        description="截图 + console + network + 关键交互审计（隔离浏览器）。",
        egress_profile="browser_egress", sandbox_profile="isolated_browser", idempotency=False,
        reconcile_strategy="none", factory=lambda: BrowserCaptureTool(),
    )

    async def run(self, ctx: ToolContext, *, url: str) -> ToolResult:
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="research",
            what="browser_capture 暂无可隔离浏览器", why="无隔离浏览器运行时",
            next="待接入隔离浏览器后可用", retryable=False, retry_scope="none"))


class ImgGenerateTool(BaseTool):
    meta = ToolMeta(
        tool_id="img_generate",
        risk=RiskLevel.MID,
        domain=Domain.RESEARCH,
        description="图像生成并保存来源/成本；当前无图像生成后端可用。",
        egress_profile="image_gen_api", sandbox_profile="egress_limited",
        max_input_bytes=4096, idempotency=True, reconcile_strategy="asset_pending",
        unknown_timeout_seconds=120, factory=lambda: ImgGenerateTool(),
    )

    async def run(self, ctx: ToolContext, *, prompt: str, session: AsyncSession | None = None
                  ) -> ToolResult:
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="research",
            what="img_generate 暂未接入图像生成后端", why="无 image-gen provider 配置",
            next="待接入图像生成服务后可用", retryable=False, retry_scope="none"))


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (
        WebSearchTool(), WebFetchTool(), RagQueryTool(), BrowserCaptureTool(), ImgGenerateTool()
    )]
