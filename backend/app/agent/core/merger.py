"""结果合并器(§多意图 v1.0)。

把多个子任务的执行结果合并为一段 **自然、连贯、完整** 的中文回复(兜底 4)。
- 成功子任务: 给出清晰结论
- 失败子任务: 明确告知哪些未完成及原因
- 失败但 partial delivery: 仍交付成功部分 + 明确失败说明

对外: ResultMerger.merge(results, original_query, model_id) -> str
"""

from __future__ import annotations

import logging

from ..core.models import SUB_BLOCKED, SUB_DONE, SUB_FAILED, SUB_SKIPPED, SubTaskResult
from ..providers import get_chat_model, resolve_fallback_order

logger = logging.getLogger("ai_service.merger")

MERGE_SYSTEM = (
    "你是结果综合助手。用户的一个请求被拆分为多个子任务并行执行, 现在需要把结果合并。\n"
    "要求:\n"
    "1. 按逻辑顺序组织(而非简单拼接), 像是一个完整回答\n"
    "2. 对成功的部分给出清晰结论(可提及产物/链接)\n"
    "3. 如有失败部分, 明确告知用户哪些未完成及原因\n"
    "4. 保持语气一致、口语自然, 用中文\n"
    "5. 不要重复『子任务1/子任务2』这类机械表述, 用自然段落\n"
    "6. 每个子任务用小标题开头, 标题格式: '## **<意图标题>**'(二级标题+加粗, 视觉上更大更醒目); "
    "标题下方用普通正文(不要加粗)给出该意图的结果。\n"
    "7. 意图标题取自各子任务类别(如 闲聊问答 / 建站生成 / 设计配色 / 联网搜索), 用中文; "
    "若标题含英文字母请统一转为大写。\n"
)

# 子任务技能 → 中文意图标题(供合并结果段落标题使用)
SKILL_TITLE = {
    "agent_chat": "闲聊问答",
    "agent_search": "联网搜索",
    "agent_build": "站点修改",
    "agent_generate_site": "建站生成",
    "agent_design": "设计配色",
    "agent_doc": "文档生成",
    "agent_delete": "删除操作",
    "explain": "解释问答",
}


class ResultMerger:
    """合并多子任务结果。"""

    async def merge(
        self,
        results: list[SubTaskResult],
        original_query: str,
        model_id: str = "deepseek",
    ) -> str:
        """LLM 合成合并结果; 异常时降级为拼接。"""
        try:
            prompt = self._build_merge_prompt(results, original_query)
            order = resolve_fallback_order(model_id)
            last_e: Exception | None = None
            for mid in order:
                try:
                    chat = get_chat_model(mid, streaming=False)
                    resp = await chat.ainvoke([
                        {"role": "system", "content": MERGE_SYSTEM},
                        {"role": "user", "content": prompt[:3000]},
                    ])
                    text = (resp.content or "").strip()
                    if text:
                        return text
                except Exception as e:
                    last_e = e
                    logger.warning("[合并] 模型%s失败: %s", mid, e)
                    continue
            if last_e:
                logger.warning("[合并] 全部模型失败: %s", last_e)
        except Exception as e:
            logger.warning("[合并] 异常: %s", e)
        return self._fallback_concat(results)

    def _build_merge_prompt(self, results: list[SubTaskResult], original_query: str) -> str:
        parts = [f"用户原始请求:\n{original_query}\n\n各子任务执行结果:\n"]
        for i, r in enumerate(results, 1):
            title = SKILL_TITLE.get(r.skill) or (r.skill or f"意图{i}")
            if r.status == SUB_DONE:
                out = r.output_text[:800] or "(已生成产物)"
                arts = " / ".join(r.artifacts) if r.artifacts else ""
                parts.append(
                    f"[{i}] 标题={title} 目标={r.goal}\n"
                    f"产出: {out}\n"
                    + (f"产物链接: {arts}\n" if arts else "")
                )
            elif r.status in (SUB_FAILED, SUB_BLOCKED, SUB_SKIPPED):
                label = "❌ 失败" if r.status == SUB_FAILED else ("⛔ 已拒绝" if r.status == SUB_BLOCKED else "⏸ 已跳过")
                parts.append(f"[{i}] 标题={title} 目标={r.goal}\n原因: {r.error}\n")
        parts.append(
            "\n请将以上合并为一段连贯中文回复。每个子任务用小标题 '## **<意图标题>**' 开头(加粗、二级标题), "
            "标题下方用普通正文给出该意图的结果, 不要加粗正文。"
        )
        return "\n".join(parts)

    def _fallback_concat(self, results: list[SubTaskResult]) -> str:
        """LLM 不可用时的降级拼接(保证总能交付, 段落标题加粗更直观)。"""
        lines = []
        for r in results:
            title = SKILL_TITLE.get(r.skill) or (r.skill or "意图")
            if r.status == SUB_DONE:
                lines.append(f"## **{title}**")
                lines.append(r.output_text[:300] or "已完成")
                if r.artifacts:
                    lines.append(f"   产物: {' / '.join(r.artifacts)}")
            elif r.status in (SUB_FAILED, SUB_BLOCKED, SUB_SKIPPED):
                label = "❌ 失败" if r.status == SUB_FAILED else ("⛔ 已拒绝" if r.status == SUB_BLOCKED else "⏸ 已跳过")
                lines.append(f"## **{title}**")
                lines.append(f"{label} {r.goal}：{r.error}")
        return "\n".join(lines)
