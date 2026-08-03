from __future__ import annotations

import logging
import time

from app.config import settings
from app.core.turn_context import TurnContext
from app.domains.chat import chat_service
from app.ragstore import retrieve as _rag_retrieve, format_hits_for_prompt as _fmt_hits

logger = logging.getLogger("app.domains.research")


class ResearchService:
    async def research(self, context: TurnContext) -> str:
        """研究域：优先检索 rag_corpus 知识库返回带引用的结论（fail-soft）。

        ⚠️ 关键改动：RAG 无命中时不再返回死板占位，而是委派给 ``chat_service.respond``——
        复用其 system 提示词中的「能力边界 + 替代方案」护栏，让 LLM 为用户给出可行方案
        （例如搜索/天气类请求，模型会说明实时数据暂不可取，并推荐公开服务或可行路径），
        而不是冷冰冰的「查不了」。RAG 命中路径零成本、低延迟，保持不变。
        """
        t0 = time.time()
        q = context.clean_message
        logger.info("[research] 研究请求 turn=%s msg=%.60s", context.turn_id, q)
        hits = await _rag_retrieve(settings.chroma_collection_rag_corpus, q, top_k=5)
        elapsed = (time.time() - t0) * 1000
        if not hits:
            logger.info("[research] 无匹配知识库, 委派 LLM 给替代方案 turn=%s (%.1fms)", context.turn_id, elapsed)
            # 正文与流式帧由 chat_service.respond 内部自行 emit + append response_fragment，
            # 故此处仅触发委派并返回空串，S6 的 _run_research 对空文本不再重复 append，避免双片段。
            await chat_service.respond(context)
            return ""
        body = _fmt_hits(hits, label="资料")
        logger.info("[research] 命中 %d 条资料 turn=%s (%.1fms)", len(hits), context.turn_id, elapsed)
        return f"基于知识库检索到 {len(hits)} 条相关资料：\n{body}\n\n（以上为向量库检索结果，供参考）"


research_service = ResearchService()
