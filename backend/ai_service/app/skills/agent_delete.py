"""agent_delete: 项目内生成物删除工具。

职责:
  1. 分析用户意图: 删全部产物 vs 删单个文件
  2. 发送确认事件(confirm SSE) → 等待用户点击确认
  3. 确认后执行 DELETE API → 返回结果

安全边界:
  - 仅允许删除项目内生成的产物文件(HTML/CSS/JS/图片等)
  - 不允许删除项目本身
  - 不允许删除非产物内容(对话/消息/用户数据)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

logger = logging.getLogger("ai_service.delete")

# 删除关键词 → 用于快速匹配
_DELETE_KW = ("删除", "删掉", "删了", "删", "移除", "清空", "去掉", "干掉")
_DELETE_ALL_KW = ("所有", "全部", "清空", "这些", "它们", "整个")

# 修改类关键词: 含这些词的不走删除工具, 走 build/modify
_MODIFY_KW = (
    "修改", "优化", "调整", "加上", "增加", "添加", "hover", "按钮", "布局",
    "导航", "footer", "配色", "颜色", "背景", "字体", "图片", "关于我", "头像",
    "社交", "版权", "响应式", "汉堡", "修复", "改成", "换成", "去掉", "去掉那个",
    "banner", "Banner", "slogan", "Slogan", "标题", "菜单", "微调", "改一下",
    "美化和", "精致", "升级",
)


def _is_delete_request(msg: str) -> bool:
    """判断是否为删除请求(排除修改类请求)。"""
    has_del = any(k in msg for k in _DELETE_KW)
    has_mod = any(k in msg for k in _MODIFY_KW)
    return has_del and not has_mod


def _is_delete_all(msg: str) -> bool:
    """判断是否为「删除全部」请求。"""
    return any(k in msg for k in _DELETE_ALL_KW)


def _extract_filename(msg: str) -> str:
    """从消息中提取文件名。"""
    import re
    m = re.search(r'(\S+\.(?:html|css|js|json|png|jpg|jpeg|gif|svg|webp|md|txt|zip|py|ts))', msg)
    if m:
        return m.group(1)
    m = re.search(r'[「『"](.+?)[」』"]', msg)
    if m:
        return m.group(1)
    return ""


async def run_delete(
    model_id: str,
    messages: list[dict],
    **kwargs,
) -> AsyncGenerator[dict[str, Any], None]:
    """agent_delete 入口(SSE 生成器)。

    接收参数(来自 Worker):
      - model_id: 模型 id
      - messages: 对话历史
      - trace_id: 跟踪 id
      - intent_info: 意图信息
      - confirmed: 是否已确认(二次请求)
      - conversation_id: 会话 id
      - project_id: 项目 id
      - site_generated: 是否已生成站点
    """
    trace_id = kwargs.get("trace_id", "")
    intent_info = kwargs.get("intent_info", {}) or {}
    confirmed = kwargs.get("confirmed", False)
    conversation_id = kwargs.get("conversation_id")
    project_id = kwargs.get("project_id")
    site_generated = kwargs.get("site_generated", False)

    # 取最后一条用户消息
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "") or ""
            break

    def ev(event: str, **kw) -> dict:
        return {"event": event, "data": kw or {}}

    # ── 非删除请求 → 降级为闲聊 ──
    if not _is_delete_request(user_msg):
        logger.info("[agent_delete] 非删除请求,降级 chat trace=%s", trace_id)
        yield ev("token", data="抱歉，我只负责项目内生成物删除。如需其他帮助请重新描述需求。")
        return

    # ── 无项目上下文或未生成站点 → 拒绝 ──
    if not site_generated:
        logger.info("[agent_delete] 无站点/项目上下文,拒绝 trace=%s", trace_id)
        yield ev("token", data="当前项目还没有生成产物哦～等你建站完成后再来找我删除文件吧。")
        return

    is_delete_all = _is_delete_all(user_msg)
    fname = _extract_filename(user_msg) if not is_delete_all else ""

    # ── 未确认 → 发送确认事件 ──
    if not confirmed:
        if is_delete_all:
            reason = "警告：你即将删除当前项目的全部生成产物，此操作不可撤销。确认继续吗？"
        elif fname:
            reason = f"你希望删除文件「{fname}」——删除后将无法恢复。确认删除吗？"
        else:
            reason = f"你希望「{user_msg[:30]}」——删除后将无法恢复。确认删除吗？"
            fname = user_msg[:30]

        logger.info("[agent_delete] 发送确认 trace=%s delete_all=%s file=%s",
                    trace_id, is_delete_all, fname)
        yield ev("confirm", reason=reason, skill="agent_delete")
        return

    # ── 已确认 → 执行删除 ──
    import aiohttp
    from ..config import settings

    biz_url = settings.business_service_url or "http://localhost:7101"

    if is_delete_all:
        url = f"{biz_url}/api/projects/{project_id}/artifacts?confirmed=true"
        method = "DELETE"
    elif fname:
        from urllib.parse import quote
        url = f"{biz_url}/api/projects/{project_id}/artifacts/files?confirmed=true&name={quote(fname)}"
        method = "DELETE"
    else:
        yield ev("token", data="未能确定要删除的目标，请换个方式说（如「删除index.html」或「删除所有产物」）。")
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url) as resp:
                data = await resp.json()
                if data.get("ok"):
                    deleted = data.get("deleted", 0)
                    if is_delete_all:
                        msg_text = f"已成功删除全部 {deleted} 个生成产物。如需重新生成网站，随时告诉我～"
                    else:
                        msg_text = f"已删除文件「{fname}」。"
                    logger.info("[agent_delete] 删除成功 trace=%s deleted=%s", trace_id, deleted)
                    yield ev("token", data=msg_text)
                    yield ev("node", stage="done")
                else:
                    logger.warning("[agent_delete] 删除失败 trace=%s resp=%s", trace_id, data)
                    yield ev("token", data=f"删除失败：{data.get('detail', '未知错误')}")
    except Exception as e:
        logger.error("[agent_delete] 删除异常 trace=%s: %s", trace_id, e)
        yield ev("token", data="删除请求未能完成，请稍后重试或检查网络连接。")


# 注册到 SkillRegistry
from ..registry import register_skill

register_skill(
    name="agent_delete",
    display_name="文件管理",
    avatar="🗑️",
    role="产物删除",
    intent_tags=["删除", "删了", "删掉", "清除", "移除"],
    handler=run_delete,
    is_graph=False,
    description="删除项目内的生成产物文件（全部或单个），需二次确认",
)
