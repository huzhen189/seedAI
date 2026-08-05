from __future__ import annotations

import logging
import time

from app.config import settings
from app.core.turn_context import TurnContext
from app.core.tool_runner import call_tool, make_tool_context
from app.domains.chat import chat_service
from app.tools.base import ToolStatus

logger = logging.getLogger("app.domains.research")


class ResearchService:
    async def research(self, context: TurnContext) -> str:
        """研究域：经统一执行器 ``call_tool("rag_query")`` 检索知识库（Phase 1 接线）。

        改动要点（相对旧实现）：
        - 不再直接 ``_rag_retrieve``，而是走 ``call_tool`` 统一执行器，获得超时护栏、
          结构化日志与（可选）W0 账本，与全仓工具调用同源。
        - ``top_k`` 统一采用 ``settings.rag_top_k``（旧实现硬编码 5，与 ``RagQueryTool``
          不一致，现已收敛到一处配置）。
        - 命中路径：取 ``ToolResult.data["text"]`` 拼结论；失败/无匹配仍 fail-soft 委派
          ``chat_service.respond``，保持「不冷冰冰占位」的体验不变。

        ⚠️ 读-only 检索默认不写 W0 账本（``ledger=False``）；如后续要审计检索量，
        可改为 ``ledger=context.db_session is not None``。
        """
        t0 = time.time()
        q = context.clean_message
        logger.info("[research] 研究请求 turn=%s msg=%.60s", context.turn_id, q)
        # 作用域隔离：只投影最小 ToolContext，绝不把整个 TurnContext 塞入工具。
        tctx = make_tool_context(context)
        res = await call_tool(
            "rag_query", tctx,
            ledger=False,
            collection=settings.chroma_collection_rag_corpus,
            query=q,
            session=context.db_session,
        )
        elapsed = (time.time() - t0) * 1000
        if res.status == ToolStatus.SUCCEEDED:
            hits = int(res.data.get("hits", 0) or 0)
            body = (res.data.get("text") or "").strip()
            # ⚠️ hits==0 / body 为空时**不能**当成功返回：否则用户会收到
            # 「基于知识库检索到 0 条相关资料：」这种空壳文案。此处并入 fail-soft 分支，
            # 由 chat_service.respond 给出真正有用的回答。
            if hits > 0 and body:
                logger.info("[research] 命中 %d 条资料 turn=%s (%.1fms)", hits, context.turn_id, elapsed)
                return f"基于知识库检索到 {hits} 条相关资料：\n{body}\n\n（以上为向量库检索结果，供参考）"
            logger.info("[research] rag_query 成功但零命中(hits=%d body_len=%d)，转委派 LLM turn=%s",
                        hits, len(body), context.turn_id)
        else:
            logger.info("[research] rag_query 失败(code=%s), 委派 LLM 替代方案 turn=%s (%.1fms)",
                        (res.error.code if res.error else "?"), context.turn_id, elapsed)
        # 失败/零命中：fail-soft 委派 LLM 给替代方案（体验不变）。
        # 正文与流式帧由 chat_service.respond 内部自行 emit + append response_fragment，
        # 故此处仅触发委派并返回空串，S6 的 _run_research 对空文本不再重复 append，避免双片段。
        await chat_service.respond(context)
        return ""


research_service = ResearchService()
