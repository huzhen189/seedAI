from __future__ import annotations

import logging
import time

from app.core.contracts import ResponseFragment, StageId
from app.core.turn_context import TurnContext
from app.llm import LLMError, chat_completion_stream

logger = logging.getLogger("app.domains.chat")

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import transaction
from app.models import Message
from app.prompts import CHAT_SYSTEM_PROMPT, CHAT_TEMPERATURE
from app.slots import SlotStack  # A 方案：分层槽位栈（引导建站提问）

# 短期记忆：每轮直接从 messages 表取本会话最近 N 条 (user+assistant) 拼进对话窗口。
# 这是真正可靠的「多轮衔接」——此前靠向量召回近似、承接相邻上一句不可靠，现降级为远场补充。
_RECENT_CHAT_LIMIT = 5

# token / think 帧前端节流：每 ≥0.2s 合并下发一次。
# 0.2s 是「实时感」与「防帧洪泛」的折中：肉眼看是连续流动(5 帧/秒)，
# 同时把 SSE 帧数压到裸流的 1/10 以内，前端 reducer 不会因高频 patch 掉帧。
# 全后端仅此一处产出 token/think 流(research 域已委托 chat_service)，改这里即全局生效。
_EMIT_INTERVAL_S = 0.2  # 提示词集中于 app/prompts


class ChatService:
    async def respond(self, context: TurnContext) -> str:
        """纯聊天回复(S6 无 plan 分支调用)。

        流式产出：逐块把 ``think``(思考过程) 与 ``token``(回复正文) 经 ``context.emit``
        实时推到前端(每 ≥0.2s 合并一次, 防帧洪泛); 同时仍把完整文本塞回 ``response_fragments``,
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
        # 多轮关联（远场补充）：S1 从向量库召回的「历史对话上下文」作为补充提示。
        # 注：真正的近场多轮衔接已由下方的「最近对话窗口」承担（查 messages 表），
        # 向量召回在此仅作远场兜底（弥补长期偏好/跨会话上下文），不再作为唯一记忆来源。
        if context.project_context:
            ctx_block = "\n".join(f"- {t}" for t in context.project_context)
            system_content += (
                "\n\n【相关历史背景（远场补充）】——以下为可能与本消息相关的历史片段，"
                "仅供必要时的背景参考，不要凭空关联到无关主题：\n"
                + ctx_block
            )
        # SIR 状态拼接（所有意图都需注入，不再限定建站）：把 S3 合并后的「已收集信息」与
        # 分层槽位栈的「待确认项」送入 prompt，让模型知晓当前会话已沉淀的事实，避免重复追问、
        # 并能延续上一轮的话题（这是进入 LLM 必做的上下文，与具体意图无关）。
        if context.understanding is not None and context.understanding.slot_stack:
            try:
                stack_obj = SlotStack.model_validate(context.understanding.slot_stack)
            except Exception:  # noqa: BLE001
                stack_obj = None
            if stack_obj is not None:
                filled = set((context.sir_after_dst.slots or {}).keys()) if context.sir_after_dst else set()
                g = stack_obj.guidance(filled)
                req_block = "、".join(g["missing_required"]) or "（已齐）"
                opt_block = "、".join(g["suggested_optional"]) or "（无）"
                system_content += (
                    "\n\n【本轮待确认的收集信息】——涉及结构化收集时，优先向用户确认以下必填项"
                    "（已收集的不必再问）：\n"
                    f"- 必填：{req_block}\n"
                    f"- 可选建议：{opt_block}\n"
                    "请用简洁提问引导用户补齐，不要一次性罗列过多。"
                )
        # 已沉淀的会话事实（SIR slots，全意图）：模型据此个性化、不重复问已表达项。
        if context.sir_after_dst and context.sir_after_dst.slots:
            known = "\n".join(f"- {k}：{v}" for k, v in context.sir_after_dst.slots.items())
            system_content += (
                "\n\n【当前会话已收集的信息】——以下事实已在前面轮次达成一致或用户已表达，"
                "请直接沿用、不要重复追问：\n"
                + known
            )
        # 拼装对话窗口：system → 最近 N 条历史(user/assistant) → 当前 user。
        # 最近对话从 messages 表按本会话实时拉取（fail-soft 降级为空），保证相邻上一句 100% 衔接；
        # 这是真正的短期记忆，优先于上面的向量召回远场补充。
        recent = await _load_recent_messages(context)
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        for role, content in recent:
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
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


async def _load_recent_messages(context: TurnContext) -> list[tuple[str, str]]:
    """取本会话最近 _RECENT_CHAT_LIMIT 条 (user+assistant) 对话，按时间升序返回。

    这是真正的「短期记忆」窗口：让 LLM 直接看到相邻上一轮（含 assistant 回复），
    从根本上解决此前「承接上一句失败」的问题。fail-soft：任何异常都降级为空列表，
    绝不能因读历史而阻断本轮回复。
    """
    # S6 执行阶段自带注入的 DB 会话（services/turns.py 构造 context 时已挂 db_session），
    # 优先复用同一事务会话，避免额外开连接；拿不到时再用只读事务兜底（仍然 fail-soft）。
    conv_id = context.session.conversation_id
    if not conv_id:
        return []
    session = context.db_session
    try:
        if session is None:
            async with transaction() as s:
                rows = await _fetch_recent(s, conv_id)
        else:
            rows = await _fetch_recent(session, conv_id)
    except Exception as exc:  # noqa: BLE001 — 旁路读取，失败不得反噬主流程
        logger.warning("[chat] 读取最近对话失败 conv=%s: %s", conv_id, exc)
        return []
    return rows


async def _fetch_recent(session: AsyncSession, conv_id: int) -> list[tuple[str, str]]:
    """按 conversation_id 取最近 _RECENT_CHAT_LIMIT 条 user/assistant（升序）。"""
    recent = (
        await session.execute(
            select(Message.role, Message.content)
            .where(
                Message.conversation_id == conv_id,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(desc(Message.id))
            .limit(_RECENT_CHAT_LIMIT)
        )
    ).all()
    # 倒序回原始时间顺序（最旧的在前），保证多轮上下文连贯。
    return [(role, content) for role, content in reversed(recent)]


def _graceful_fallback(user_text: str) -> str:
    if not user_text:
        return "你好，我是 SeedAI 建站助手。告诉我你想做什么网站，我来帮你规划与生成。"
    return (
        f"我已经收到你的消息：「{user_text}」。"
        "当前模型服务暂时不可用，稍后重试即可；如果你是想建站，直接告诉我行业和想要的风格就行。"
    )


chat_service = ChatService()
