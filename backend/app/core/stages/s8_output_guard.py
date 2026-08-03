from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

from app.core.contracts import GuardResult, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S8OutputGuardStage(BaseStage):
    """S8 输出护栏(§5.6,确定性)。

    汇总所有 response_fragments 拼成回复草稿,并按校验状态改写(等待审批/被阻止的专用文案)。
    最后做确定性清洗:任何 ``<script`` 均转义为 ``&lt;script``,确保模型/provider 原始文本
    不会绕过护栏进入最终回复(规范:原始生成文本不得直抵此层)。
    """

    stage_id = StageId.S8

    async def run(self, context: TurnContext):
        fragments = [fragment.text for fragment in context.response_fragments if fragment.text and fragment.status != "info"]
        if context.validation is not None and context.validation.status == "needs_approval":
            context.reply_draft = "此操作正在等待审批。"
        elif context.validation is not None and context.validation.status == "block":
            context.reply_draft = "该操作已被安全策略阻止。"
        elif context.validation is not None and context.validation.status == "clarify":
            context.reply_draft = next(
                (f.text for f in (context.validation.response_fragments or []) if f.text),
                "我不太确定您的意图，能否再补充说明一下您想做什么？",
            )
        else:
            # 防御：若上游某环节对纯 chat 重复 append 了相同 fragment（如兜底分支与
            # _run_chat 同轮各写一次），会在拼装时出现「相同文本重复两遍」的脏正文。
            # 这里按「相邻相同段」去重，兜底保证最终落库正文不重复（不丢内容顺序）。
            deduped: list[str] = []
            for frag in fragments:
                if deduped and frag == deduped[-1]:
                    continue
                deduped.append(frag)
            # info 类进度片段不进正文（避免「已完成 x/y 项」混进回复），仅作内部 trace。
            body = "\n\n".join(deduped) or "已完成本轮处理。"
            # 社交前缀只前置一次（由 S2 剥离收集，避免每条 chat 动作都重复寒暄）。
            if context.social_prefix:
                body = f"{context.social_prefix}\n\n{body}"
            context.reply_draft = body
        # Deterministic output guard: model/provider raw text never reaches this layer.
        context.reply_final = context.reply_draft.replace("<script", "&lt;script")
        context.guard_result = GuardResult(status="passed")
        logger.info("[S8] 输出护栏通过 turn=%s fragments=%d final_len=%d",
                    context.turn_id, len(context.response_fragments), len(context.reply_final))
        return self.result(StageStatus.COMPLETED, "deterministic_output_guard")
