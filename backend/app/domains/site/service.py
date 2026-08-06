"""Site 域服务入口：编排 SiteWorkflow 的 Spec→Produce→Verify→Preview。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.tool_runner import call_tool, make_tool_context
from app.core.turn_context import TurnContext
from app.domains.site.workflow import site_workflow
from app.models import Artifact, Project
from app.ragstore import safe_upsert_bg as _rag_upsert_bg
from app.tools.base import ToolStatus

logger = logging.getLogger("app.domains.site.service")


class SiteService:
    async def create_or_edit(
        self,
        session: AsyncSession,
        context: TurnContext,
        *,
        on_chunk: "callable[[str, str], None] | None" = None,
    ) -> tuple[Artifact, str]:
        """建站/改站主入口(S6 调用)。

        流程：取项目 → build_spec(合并需求) → produce(生成 HTML) → verify(校验/最多一次修复)
        → preview(落不可变 Artifact)。任一前置失败(项目不存在/越权)直接抛错,由上层转失败响应。

        **回溯锁定**：``context.prior_project_id`` 非空（correct/supplement 且上一轮有产物）时，
        强制以该 project 为目标，保证"改上一句"落在原站上做 version+1 的受控 edit，
        而不是在当前会话默认项目上另起新站。

        Args:
            session: 数据库会话(本方法内所有写操作在同一事务,提交由调用方控制)。
            context: 本轮 TurnContext(含 project_id / user_id)。
            on_chunk: 可选流式回调 ``(kind, text)``，kind ∈ {"think","token"}，
                来自 LLM 真实生成站点时的逐块输出；S6 接成 SSE 帧让前端「构建网站」上方
                小窗实时滚动展示。不传则无流式(纯模板/修复轮)。
        Returns:
            ``(Artifact, 文本摘要)``。
        """
        target_project_id = context.prior_project_id or context.session.project_id
        project = await session.get(Project, target_project_id)
        if project is None or project.user_id != context.user.user_id:
            logger.warning("[site] 项目不存在或越权 project=%s user=%s", target_project_id, context.user.user_id)
            raise ValueError("目标项目不存在或无权访问")

        # pin 标量：project 可能在建站流程中被同会话的 expire_all 误伤失效，
        # 趁刚 load 完(greenlet 活跃)把后续需要的属性一次性读出，之后日志等处用局部变量，不再碰 project.*。
        project_id = project.id
        user_id = project.user_id
        head_artifact_id = project.head_artifact_id
        lock_version = project.lock_version
        site_spec = project.site_spec

        is_retro = context.prior_project_id is not None
        logger.info(
            "[site] 开始%s project=%s turn=%s prior_turn=%s",
            "回溯改站" if is_retro else "建站", project_id, context.turn_id, context.prior_turn_id,
        )
        # 作用域隔离：只取本子任务(site)相关的槽位，不把整轮 TurnContext 全量塞入，防污染。
        from app.core.context_scope import relevant_slots
        scoped_slots = relevant_slots(getattr(context.sir_after_dst, "slots", None), "site")
        spec = await site_workflow.build_spec(session, project, context, scoped_slots=scoped_slots)

        # ReAct 双计数器循环（docs/06 方案 A）：
        # - Action(代码执行): produce→verify 迭代，受 site_react_max_rounds_code 约束。
        # - Thought(对话/LLM 升级): 受 site_react_max_rounds_chat 约束（通路 B 未接入，先作闸门 seam）。
        # 循环内只线程 spec + 修复原因，绝不把 TurnContext 丢进 produce/verify。
        max_code = settings.site_react_max_rounds_code
        max_chat = settings.site_react_max_rounds_chat

        html = await site_workflow.produce(spec, model=context.model, on_chunk=on_chunk)
        ok, reason = site_workflow.verify(html)
        round_no = 0
        chat_rounds = 0
        last_reason = reason
        while not ok and round_no < max_code:
            round_no += 1
            logger.warning("[site] 第%d轮代码执行(produce/verify)未过 reason=%s,定向修复", round_no, last_reason)
            repair_spec = {**spec, "_repair_round": round_no, "_repair_reason": last_reason}
            html = await site_workflow.produce(repair_spec, model=context.model, on_chunk=on_chunk)
            ok, reason = site_workflow.verify(html)
            if ok:
                break
            last_reason = reason
            # Thought(对话/LLM 升级) 闸门 seam：达到上限则不再尝试 LLM 升级（当前通路 B 未接入，
            # 仅作上限占位，不在此 break，避免与代码执行轮数耦合）。
            chat_rounds = min(chat_rounds + 1, max_chat)

        if not ok:
            # 随生产补充：把失败原因沉淀进 error_patterns（后台，不阻塞）。
            asyncio.create_task(_rag_upsert_bg(
                settings.chroma_collection_error_patterns,
                [last_reason],
                metadatas=[{"kind": "auto", "sig": last_reason, "theme": spec.get("theme")}],
                id_prefix="err",
            ))
            logger.error("[site] 修复后仍校验失败 reason=%s rounds=%d", last_reason, round_no)
            raise ValueError(f"站点产物校验未通过：{last_reason}")

        artifact, text = await self._publish_preview(session, project, context, html)
        # 随生产补充知识库（后台写回，fail-soft，不阻塞用户响应）。
        _schedule_site_writeback(context, project, spec, html, artifact.version)
        logger.info("[site] 建站完成 project=%s artifact=%s v=%s", project_id, artifact.id, artifact.version)
        return artifact, text

    async def _publish_preview(
        self, session: AsyncSession, project: Project, context: TurnContext, html: str,
    ) -> tuple[Artifact, str]:
        """经统一执行器 ``call_tool("site_publish")`` 落不可变预览（Phase 4）。

        为什么不直接调 ``site_workflow.preview``：
        - 走 ``call_tool`` 才会写 **W0 操作账本**（``tool_calls``），使「常规建站发布」
          与「审批通过后由 decide_approval 执行的发布」落在同一本账上，可对账、可重放；
        - 顺带获得超时护栏与 ``site_publish`` 自带的 ``retry_policy``（io_error 重试 1 次）。

        幂等键：``site_publish:p{project_id}:{html_sha1_16}``。显式指定而不靠默认
        args_hash，是为了让「同一项目、同一份 HTML 的重复发布」稳定命中同一条账本记录，
        且天然按项目隔离（不同项目即便 HTML 完全相同也不会串号）。

        Returns:
            ``(Artifact, 面向用户的文本摘要)``。工具失败时抛 ``ValueError``，
            由 S6 的 ``_run_one`` 统一收口成 error fragment。
        """
        html_sig = hashlib.sha1(html.encode("utf-8")).hexdigest()[:16]
        project_id = project.id  # pin：避免 except 中访问已失效 project.* 触发次级 MissingGreenlet，掩盖真错误。
        idem = f"site_publish:p{project_id}:{html_sig}"
        tctx = make_tool_context(context, project_id=project_id)
        res = await call_tool(
            "site_publish",
            tctx,
            session=session,
            turn_id=context.turn_id,
            fencing_token=getattr(context, "fencing_token", None),
            idempotency_key=idem,
            # 工具自身需要的依赖显式传入（作用域隔离：不塞整轮状态，只给它要的四样）。
            project=project,
            turn_context=context,
            html=html,
        )
        if res.status != ToolStatus.SUCCEEDED:
            code = res.error.code if res.error else "site_publish_failed"
            why = res.error.why if res.error else ""
            logger.error("[site] 预览发布失败 project=%s code=%s why=%s", project_id, code, why[:200])
            raise ValueError(f"站点发布失败：{code}")

        artifact_id = res.data.get("artifact_id")
        artifact = await session.get(Artifact, artifact_id) if artifact_id else None
        if artifact is None:
            # 理论不可达：工具已成功写库并 flush，identity map 必然命中。
            logger.error("[site] 发布成功但取不到 Artifact id=%s project=%s", artifact_id, project_id)
            raise ValueError("站点发布异常：产物记录缺失")
        return artifact, str(res.data.get("message") or "")


def _schedule_site_writeback(context: TurnContext, project: Project, spec: dict,
                             html: str, version: int) -> None:
    """建站成功后后台沉淀知识：组件库 / 项目记忆 / 项目代码 / 用户偏好。

    全部走 fire-and-forget，幂等（id 含 project/用户 维度 + 内容 hash），
    不阻塞响应，也不因写入失败影响主流程。
    """
    uid = context.user.user_id
    pid = project.id
    theme = spec.get("theme", "system")
    site_type = spec.get("site_type", "")
    sections = spec.get("sections") or []
    styles = spec.get("styles") or []

    # 1) 组件库：抽取 features 区块作为可复用组件片段。
    m = re.search(r'<div class="grid">(.*?)</div>', html, re.S)
    comp_snippet = m.group(1).strip() if m else ""
    if comp_snippet:
        asyncio.create_task(_rag_upsert_bg(
            settings.chroma_collection_components,
            [comp_snippet],
            metadatas=[{"kind": "auto", "theme": theme, "type": site_type}],
            id_prefix="auto",
        ))

    # 2) 项目记忆：spec 摘要（文本，便于召回时语义检索）。
    summary = f"项目《{project.name}》类型={site_type} 主题={theme} 板块={sections} 风格={styles}"
    asyncio.create_task(_rag_upsert_bg(
        settings.chroma_collection_project_memory,
        [summary],
        metadatas=[{"kind": "spec", "project_id": pid, "version": version}],
        id_prefix=f"pm_{pid}",
    ))

    # 3) 项目代码：HTML 片段（前 4000 字符，控制体积）。
    asyncio.create_task(_rag_upsert_bg(
        settings.chroma_collection_project_code,
        [html[:4000]],
        metadatas=[{"kind": "html", "project_id": pid, "version": version}],
        id_prefix=f"pc_{pid}",
    ))

    # 4) 用户偏好：主题 / 风格（仅当用户显式表达时）。
    prefs: list[str] = []
    if theme and theme != "system":
        prefs.append(f"偏好主题={theme}")
    if styles:
        prefs.append("偏好风格=" + "、".join(styles[:5]))
    if prefs:
        asyncio.create_task(_rag_upsert_bg(
            settings.chroma_collection_user_preferences,
            prefs,
            metadatas=[{"kind": "preference", "user_id": uid} for _ in prefs],
            id_prefix=f"up_{uid}",
        ))


# 模块级单例（此前这里被重复赋值两次 —— 后一次会覆盖前一次，
# 任何在两行之间持有旧引用的模块都会拿到「另一个」实例，状态无法共享。
# 现收敛为唯一一行）。
site_service = SiteService()
