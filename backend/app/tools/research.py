"""研究/检索类原子工具（规范 §9.2 + §8.3）。

web_search / web_fetch / browser_capture / rag_query 当前**无后端联网或 Chroma 检索服务**
可用：这些 Tool 的契约（风险、沙箱、出口、reconcile）已登记，但运行时在无对应
后端时**明确返回 failed**，绝不静默成功或伪造结果（§9.2：Tool 必须返回 ToolResult）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import Domain, ErrorEnvelope, RiskLevel
from app.config import settings
from app.ragstore import retrieve as _rag_retrieve, format_hits_for_prompt as _fmt_hits
from app.tools._registry import ToolMeta
from app.tools.base import BaseTool, ToolContext, ToolResult

import logging

logger = logging.getLogger("app.tools.research")

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
        """搜索并返回来源元数据(§8.3 web_search, low)。

        本环境无搜索后端,按 §9.2「Tool 必须返回 ToolResult」约束,**明确返回 failed**
        并说明原因,绝不静默成功或伪造结果。
        """
        logger.debug("[web_search] query=%s (后端未接入,返回 unavailable)", query[:80])
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
        """受限抓取(§8.3 web_fetch, low)。

        本环境无 fetch 客户端后端,返回 failed(不静默)。投产时应在此处做
        SSRF/DNS rebinding 防护、大小/域名/超时控制。
        """
        logger.debug("[web_fetch] url=%s (后端未接入,返回 unavailable)", url[:120])
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
        """Chroma 作用域隔离检索(§8.3 rag_query, low)。

        按 collection 做语义检索；无匹配不静默成功，返回明确 failed。
        """
        hits = await _rag_retrieve(collection, query, top_k=settings.rag_top_k)
        if not hits:
            logger.debug("[rag_query] collection=%s query=%s 无匹配", collection, query[:80])
            return ToolResult.fail(ErrorEnvelope(
                code="no_match", category="memory",
                what=f"rag_query 在集合 {collection} 无相关结果", why="向量库无匹配",
                next="尝试调整查询或补充知识库", retryable=False, retry_scope="none"))
        text = _fmt_hits(hits, label=collection)
        logger.info("[rag_query] collection=%s 命中 %d 条", collection, len(hits))
        return ToolResult.ok(data={"text": text, "hits": len(hits)})


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
        """隔离浏览器截图/审计(§8.3 browser_capture, low)。

        本环境无隔离浏览器运行时,返回 failed(不静默)。投产时应返回
        screenshot+console+network+交互审计产物。
        """
        logger.debug("[browser_capture] url=%s (后端未接入,返回 unavailable)", url[:120])
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
        """图像生成(§8.3 img_generate, mid)。

        本环境无 image-gen provider,返回 failed(不静默)。投产时应调用图像生成
        API 并保存来源/成本元数据(asset_pending reconcile)。
        """
        logger.debug("[img_generate] prompt=%s (后端未接入,返回 unavailable)", prompt[:80])
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="research",
            what="img_generate 暂未接入图像生成后端", why="无 image-gen provider 配置",
            next="待接入图像生成服务后可用", retryable=False, retry_scope="none"))


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (
        WebSearchTool(), WebFetchTool(), RagQueryTool(), BrowserCaptureTool(), ImgGenerateTool()
    )]
