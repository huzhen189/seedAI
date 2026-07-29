"""Skill 运行包装(§5.2 / §5.5)。"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any, Callable, Dict, Optional

from ..events import ev
from ..registry import SkillRegistry


logger = logging.getLogger("ai_service.runner")


async def _rag_context_for_answer(
    messages: list, user_id, project_id
) -> tuple[dict, str]:
    """为 chat/search 类技能检索向量记忆, 返回 (hits_dict, context_str)。

    向量库真实作用于回答链路(修复 #V1): 让 QA/搜索类技能也能召回
    项目记忆 / 用户偏好 / 历史错误模式。无命中返回 ({}, "")。
    检索失败一律降级为空(不影响主流程)。
    """
    try:
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = (m.get("content") or "")
                break
        if not last_user:
            return {}, ""
        from ..knowledge.chroma import (
            retrieve_error_patterns,
            retrieve_project_memory,
            retrieve_user_preferences,
        )
        hits: dict[str, int] = {}
        chunks: list[str] = []
        try:
            pm = retrieve_project_memory(project_id or 0, last_user, top_k=3)
            if pm:
                hits["project_memory"] = len(pm)
                chunks.append(
                    "【项目记忆】\n" + "\n".join(f"- {x.get('content', '')[:300]}" for x in pm[:3])
                )
        except Exception as _e:  # noqa: BLE001
            logger.debug("[Runner] 项目记忆检索失败(忽略): %s", _e)
        try:
            up = retrieve_user_preferences(user_id or 0, last_user, top_k=3)
            if up:
                hits["user_pref"] = len(up)
                chunks.append(
                    "【用户偏好】\n" + "\n".join(f"- {x.get('content', '')[:300]}" for x in up[:3])
                )
        except Exception as _e:  # noqa: BLE001
            logger.debug("[Runner] 用户偏好检索失败(忽略): %s", _e)
        try:
            ep = retrieve_error_patterns(last_user, top_k=3)
            if ep:
                hits["error_pattern"] = len(ep)
                chunks.append(
                    "【历史错误模式】\n" + "\n".join(f"- {x.get('content', '')[:300]}" for x in ep[:3])
                )
        except Exception as _e:  # noqa: BLE001
            logger.debug("[Runner] 错误模式检索失败(忽略): %s", _e)
        if not hits:
            return {}, ""
        return hits, "\n\n".join(chunks)
    except Exception as e:  # noqa: BLE001
        logger.debug("[Runner] RAG 增强失败(忽略): %s", e)
        return {}, ""


async def run_skill(
    skill_name: str,
    model_id: str,
    messages: list,
    *,
    trace_id: Optional[str] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    intent_info: Optional[dict] = None,
    **extra_kwargs,  # 透传: requirement_doc, project_status, conversation_summary 等
) -> AsyncGenerator[Dict[str, Any], None]:
    """统一入口:意图 → 分发 → Skill 执行 → done。

    精细日志(skill/agent 入参出参):
    - [入参] 打印本次执行的完整入参结构体(skill/model/意图/透传参数/消息数)。
    - [出参] 打印产出事件数 + 文本产出摘要(截断)。
    §4 角色编排:按技能映射角色,记录 ai:role:* 统计(单一路径,避免双记)。
    """
    # ── [入参] 精细日志:完整入参结构体 ──
    _extra_keys = {k: (f"<{type(v).__name__}>" if not isinstance(v, (str, int, float, bool, list, dict)) else v)
                   for k, v in extra_kwargs.items()}
    logger.info(
        "[Runner][入参] skill=%s model=%s trace=%s msgs=%d intent_info=%s extra_kwargs=%s",
        skill_name, model_id, trace_id, len(messages),
        intent_info, _extra_keys,
    )
    logger.info(
        "▶ 开始执行 trace=%s skill=%s model=%s intent=%s/%s msgs=%d",
        trace_id, skill_name, model_id,
        intent_info.get("level1") if intent_info else "?",
        intent_info.get("level2") if intent_info else "?",
        len(messages),
    )
    yield ev("node", stage="enter_router", agent_id=skill_name)

    # 意图信息透传给前端(两级 + 行业)
    if intent_info:
        yield ev(
            "intent",
            level1=intent_info.get("level1"),
            level2=intent_info.get("level2"),
            label=intent_info.get("label"),
            level1_label=intent_info.get("level1_label"),
            level2_label=intent_info.get("level2_label"),
            confidence=intent_info.get("confidence"),
            industry=intent_info.get("industry"),
            decision=intent_info.get("decision"),
            risk_level=intent_info.get("risk_level"),
            requires_confirm=intent_info.get("requires_confirm"),
            selected_skill=intent_info.get("selected_skill"),
            plan=intent_info.get("plan"),
            sub_tasks=intent_info.get("sub_tasks"),
            split_reason=intent_info.get("split_reason", ""),
            agent_id=skill_name,
        )

    # unsupported: 直接返回提示
    if intent_info and intent_info.get("level1") == "unsupported":
        logger.info("◼ trace=%s 不支持该意图, 返回提示", trace_id)
        yield ev("node", stage="unsupported", message="暂不支持此功能, 请尝试其他类型请求")
        yield ev("done")
        return

    entry = SkillRegistry.get(skill_name)
    if entry is None:
        logger.warning("trace=%s Skill '%s' 未注册", trace_id, skill_name)
        yield ev("error", message=f"Skill '{skill_name}' 不存在")
        yield ev("done")
        return

    logger.info(
        "▸ trace=%s 分发到 Skill: %s(is_graph=%s)",
        trace_id, entry.name, entry.is_graph,
    )
    yield ev("node", stage="dispatch", skill=entry.name, agent_id=skill_name)

    # ── RAG 增强(向量库真实作用于回答, 修复 #V1): chat/search 技能注入向量记忆 ──
    rag_context = ""
    rag_hits: dict = {}
    if entry.name in ("agent_chat", "agent_search"):
        rag_hits, rag_context = await _rag_context_for_answer(
            messages, extra_kwargs.get("user_id"), extra_kwargs.get("project_id"))
        if rag_hits:
            yield ev(
                "think", stage="rag", hits=rag_hits,
                msg=f"向量召回 {sum(rag_hits.values())} 条相关记忆, 注入回答上下文",
            )
    rag_kw = {"rag_context": rag_context} if rag_context else {}

    # 参数透传(供 handler 按 level2/industry 调整行为)
    level2 = intent_info.get("level2") if intent_info else None
    industry = intent_info.get("industry", "other") if intent_info else "other"
    intent_val = intent_info.get("level1") if intent_info else None
    logger.info(
        "[Runner] [1/3] 分发 skill=%s 意图=%s/%s 行业=%s is_graph=%s doc=%s status=%s summary=%s",
        entry.name, intent_val or "-", level2 or "-", industry,
        entry.is_graph,
        "有" if extra_kwargs.get("requirement_doc") else "无",
        extra_kwargs.get("project_status", "?"),
        "有" if extra_kwargs.get("conversation_summary") else "无",
    )

    handler = entry.handler
    t0 = time.time()
    event_cnt = 0
    out_buf: list[str] = []  # 收集文本产出,供出参日志摘要
    _ok = True
    try:
        if entry.is_graph or inspect.isasyncgenfunction(handler):
            logger.info("[Runner] [2/3] 开始执行 skill=%s (async生成器)", entry.name)
            async for item in handler(
                model_id=model_id,
                messages=messages,
                trace_id=trace_id,
                is_cancelled=is_cancelled,
                intent=intent_val,
                level2=level2,
                industry=industry,
                **extra_kwargs, **rag_kw,
            ):
                event_cnt += 1
                if isinstance(item, dict) and "event" in item:
                    if item.get("event") == "token":
                        d = item.get("data")
                        if isinstance(d, str):
                            out_buf.append(d)
                    yield item
                else:
                    out_buf.append(item if isinstance(item, str) else str(item))
                    yield ev("token", data=item if isinstance(item, str) else str(item))
        else:
            logger.info("[Runner] [2/3] 开始执行 skill=%s (同步)", entry.name)
            result = await handler(model_id=model_id, messages=messages, trace_id=trace_id, **rag_kw)
            event_cnt += 1
            if isinstance(result, dict) and "event" in result:
                yield result
            else:
                out_buf.append(result if isinstance(result, str) else str(result))
                yield ev("token", data=result if isinstance(result, str) else str(result))
    except Exception as e:
        _ok = False
        elapsed = (time.time() - t0) * 1000
        logger.error("[Runner] skill=%s 执行异常 耗时=%.0fms 错误=%s: %s",
                    entry.name, elapsed, type(e).__name__, e)
        # 兜底: 任何 skill 执行失败都产出 refined 终版文案(被前端 onRefined 落库为正式回复,
        # 用户必见且刷新仍在), 同时 yield error 事件让前端感知失败态(超时/降级提示)。
        try:
            from ..skills._llm_fallback import emit_llm_failure
            async for _ev in emit_llm_failure(model_id, e, skill_name):
                yield _ev
        except Exception as _fe:  # noqa: BLE001
            # 兜底中的兜底: 极端情况下至少给一条 error 提示
            logger.error("[Runner] 失败兜底事件产出异常(忽略): %s", _fe)
            yield ev("refined", data=f"😔 抱歉，刚才请求处理时出现问题，没能生成内容。请稍后重试，或换一个模型试试。", agent_id=skill_name)
        yield ev("error", message=f"{type(e).__name__}: {e}")
    elapsed = (time.time() - t0) * 1000
    # ── [出参] 精细日志:事件数 + 文本产出摘要 ──
    out_text = "".join(out_buf)
    logger.info("[Runner][出参] skill=%s 事件数=%d 文本长度=%d 摘要=%.200s",
                entry.name, event_cnt, len(out_text), out_text.replace("\n", " "))
    logger.info("[Runner] [3/3] 执行完毕 skill=%s 事件数=%d 耗时=%.0fms", entry.name, event_cnt, elapsed)
    # ── §4 角色编排统计(单一路径记录,避免双记) ──
    try:
        from ..roles.handoff import map_skill_to_role
        from ..analytics import record_role_dispatch
        role = map_skill_to_role(skill_name)
        if role:
            status = "done" if _ok else "failed"
            await record_role_dispatch(role, skill_name, status, elapsed)
    except Exception as _re:  # noqa: BLE001
        logger.debug("[Runner] 角色统计记录失败(忽略): %s", _re)
    yield ev("done")
