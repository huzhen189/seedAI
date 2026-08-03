from __future__ import annotations

import logging
import time

from app.core.contracts import ResponseFragment, StageId
from app.core.turn_context import TurnContext
from app.llm import LLMError, chat_completion_stream

logger = logging.getLogger("app.domains.chat")

from app.prompts import CHAT_SYSTEM_PROMPT, CHAT_TEMPERATURE
from app.slots import SlotStack  # A 方案：分层槽位栈（引导建站提问）

# token / think 帧前端节流：每 ≥1.0s 合并下发一次，避免高吞吐时帧洪泛导致前端卡顿/丢失。
_EMIT_INTERVAL_S = 1.0  # 提示词集中于 app/prompts


class ChatService:
    async def respond(self, context: TurnContext) -> str:
        """纯聊天回复(S6 无 plan 分支调用)。

        流式产出：逐块把 ``think``(思考过程) 与 ``token``(回复正文) 经 ``context.emit``
        实时推到前端(每 ≥1s 合并一次, 防帧洪泛); 同时仍把完整文本塞回 ``response_fragments``,
        保证 S8 汇总与会话落库逻辑不变(向后兼容)。LLM 不可用时降级到本地静态回复。
        """
        user_text = context.clean_message or ""
        logger.debug("[chat] 生成闲聊回复(流式) msg=%.60s user_context=%s", user_text, context.user_context)
        # 把 S1 按 user_id 召回的用户偏好/属性拼进 system prompt，实现「每请求前取信息填充」：
        # 模型据此个性化，且不再重复追问用户已表达过的项（如主题/风格/品牌色）。
        system_content = CHAT_SYSTEM_PROMPT
        if context.user_context:
            prefs_block = "\n".join(f"- {t}" for t in context.user_context)
            system_content += (
                "\n\n【已知该用户的偏好与历史信息】——用于个性化回复，以下已提供的项不要再重复追问：\n"
                + prefs_block
            )
        # 多轮关联：把 S1 从向量库召回的「历史对话上下文」注入 prompt，让模型能承接上一轮。
        # 这是之前一直没接上的环节（project_context 召回后未被消费），导致 Agent 像没记忆一样。
        if context.project_context:
            ctx_block = "\n".join(f"- {t}" for t in context.project_context)
            system_content += (
                "\n\n【与本消息相关的历史对话片段】——用户可能在本轮承接、追问或纠正上一轮的话题，"
                "请结合这些上下文连贯作答，不要忽略前文已达成一致的内容：\n"
                + ctx_block
            )
        # A 方案：若本轮带建站意图且已拼装分层槽位栈，把「待收集必填/可选槽」注入 prompt，
        # 引导模型向用户补齐信息（已填的不必再问），解决「建站太随意、不收集信息」的问题。
        if context.understanding is not None and context.understanding.slot_stack:
            try:
                stack_obj = SlotStack.model_validate(context.understanding.slot_stack)
            except Exception:  # noqa: BLE001
                stack_obj = None
            if stack_obj is not None:
                has_site = any(r.domain.value == "site" for r in context.understanding.resolved_intents)
                if has_site:
                    filled = set((context.sir_after_dst.slots or {}).keys()) if context.sir_after_dst else set()
                    g = stack_obj.guidance(filled)
                    req_block = "、".join(g["missing_required"]) or "（已齐）"
                    opt_block = "、".join(g["suggested_optional"]) or "（无）"
                    system_content += (
                        "\n\n【本次建站待收集信息】优先向用户确认以下必填项（已填的不必再问）：\n"
                        f"- 必填：{req_block}\n"
                        f"- 可选建议：{opt_block}\n"
                        "请用简洁提问引导用户补齐，不要一次性罗列过多。"
                    )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_text},
        ]
        try:
            text_parts: list[str] = []
            think_parts: list[str] = []
            tok_buf = ""
            think_buf = ""
            last_tok = 0.0
            last_think = 0.0
            async for ev in chat_completion_stream(
                messages, temperature=CHAT_TEMPERATURE, max_tokens=768, timeout=30.0
            ):
                now = time.time()
                if ev["kind"] == "think":
                    think_buf += ev["text"]
                    think_parts.append(ev["text"])
                    if now - last_think >= _EMIT_INTERVAL_S and think_buf:
                        await context.emit("think", {"text": think_buf})
                        think_buf = ""
                        last_think = now
                else:
                    tok_buf += ev["text"]
                    text_parts.append(ev["text"])
                    if now - last_tok >= _EMIT_INTERVAL_S and tok_buf:
                        await context.emit("token", {"text": tok_buf})
                        tok_buf = ""
                        last_tok = now
            # 冲刷剩余缓冲, 保证不丢尾帧
            if tok_buf:
                await context.emit("token", {"text": tok_buf})
            if think_buf:
                await context.emit("think", {"text": think_buf})

            full = "".join(text_parts).strip()
            if full:
                context.response_fragments.append(
                    ResponseFragment(status="success", text=full, producer_stage=StageId.S6)
                )
            return full
        except LLMError as exc:
            logger.warning("LLM 闲聊流式调用失败，使用降级回复: %s", exc)
            return _graceful_fallback(user_text)

    async def health(self) -> bool:
        from app.llm import get_llm_client

        return get_llm_client().available


def _graceful_fallback(user_text: str) -> str:
    if not user_text:
        return "你好，我是 SeedAI 建站助手。告诉我你想做什么网站，我来帮你规划与生成。"
    return (
        f"我已经收到你的消息：「{user_text}」。"
        "当前模型服务暂时不可用，稍后重试即可；如果你是想建站，直接告诉我行业和想要的风格就行。"
    )


chat_service = ChatService()
