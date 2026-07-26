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

# 项目级删除信号(18 号规则: 项目不可删, 仅可删内部产物)
# 注意: 单字"站"易与"页面/首页"混淆, 故仅纳入带限定的短语(整个站/这个站/整个网站/网站本身)
_PROJECT_KW = ("项目", "整个项目", "这个站", "那个站", "整个站", "整个网站", "工程", "网站本身", "把站")
# 产物限定词(含义上把"项目"缩小到"项目内产物/文件", 不算删项目)
_PRODUCT_LIMIT_KW = ("产物", "文件", "里面的", "内的", "里面", "页面", "代码", "生成的")

# 修改类关键词: 含这些词的不走删除工具, 走 build/modify
_MODIFY_KW = (
    "修改", "优化", "调整", "加上", "增加", "添加", "hover", "按钮", "布局",
    "导航", "footer", "配色", "颜色", "背景", "字体", "图片", "关于我", "头像",
    "社交", "版权", "响应式", "汉堡", "修复", "改成", "换成", "去掉", "去掉那个",
    "banner", "Banner", "slogan", "Slogan", "标题", "菜单", "微调", "改一下",
    "美化和", "精致", "升级",
)

# 页面/模块级语义词(删除"某页/某模块" → 标记为待办, 不直接删)
_PAGE_KW = ("首页", "主页", "关于页面", "关于页", "登录页", "产品页", "联系页", "页面", "模块", "章节", "板块")


def _is_delete_request(msg: str) -> bool:
    """判断是否为删除请求(排除修改类请求)。"""
    has_del = any(k in msg for k in _DELETE_KW)
    has_mod = any(k in msg for k in _MODIFY_KW)
    return has_del and not has_mod


def _is_delete_all(msg: str) -> bool:
    """判断是否为「删除全部产物」请求。"""
    return any(k in msg for k in _DELETE_ALL_KW)


def _is_project_delete(msg: str) -> bool:
    """判断是否为『删除项目本身』(18 号: 必须 block)。

    含项目级关键词(项目/工程/整个站/整个网站) 且 未被产物限定词缩小到"项目内部"。
    """
    has_proj = any(k in msg for k in _PROJECT_KW)
    if not has_proj:
        return False
    has_limit = any(k in msg for k in _PRODUCT_LIMIT_KW)
    return not has_limit


def _is_delete_page(msg: str) -> bool:
    """是否指向"某个页面/模块"(删除页面 → 标记待办, 非直接物理删)。"""
    return any(k in msg for k in _PAGE_KW)


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

    # ── 删除项目本身 → 直接 block(18 号规则, 仅可删内部产物) ──
    if _is_project_delete(user_msg):
        logger.info("[agent_delete] 命中删项目信号, block trace=%s", trace_id)
        yield ev("block", reason="项目本身不可删除（涉及对话、统计与协作数据）。"
                                 "如需移除，请到『设置 → 删除项目』中走软删除流程；"
                                 "我可以帮你删除项目内的生成产物文件。")
        return

    # ── 无项目上下文或未生成站点 → 拒绝 ──
    if not site_generated:
        logger.info("[agent_delete] 无站点/项目上下文,拒绝 trace=%s", trace_id)
        yield ev("token", data="当前项目还没有生成产物哦～等你建站完成后再来找我删除文件吧。")
        return

    is_delete_all = _is_delete_all(user_msg)
    is_page = _is_delete_page(user_msg)
    fname = _extract_filename(user_msg) if not is_delete_all else ""

    # ── 四类边界判定(OPTIMIZE_PLAN §3): 决定 risk_level + 行为 ──
    if is_delete_all:
        risk_level = "high"          # 删全部产物 → 高风险
    elif is_page:
        # 删页面/模块: 标记待办(不直接物理删), 中等风险确认
        risk_level = "medium"
    elif fname:
        risk_level = "medium"        # 删单个文件 → 中风险
    else:
        risk_level = "medium"        # 兜底: 目标不明确 → 中风险先确认

    # ── 未确认 → 发送确认事件(带 risk_level, 前端按级别渲染) ──
    if not confirmed:
        if is_delete_all:
            reason = "警告：你即将删除当前项目的【全部】生成产物，此操作不可撤销。确认继续吗？"
        elif is_page:
            reason = f"你希望删除「{user_msg[:30]}」——按页删需重建站点。先标记为待办，确认后我再处理？"
        elif fname:
            reason = f"你希望删除文件「{fname}」——删除后将无法恢复。确认删除吗？"
        else:
            reason = f"你希望「{user_msg[:30]}」——删除后将无法恢复。确认删除吗？"
            fname = user_msg[:30]

        logger.info("[agent_delete] 发送确认 trace=%s delete_all=%s page=%s file=%s risk=%s",
                    trace_id, is_delete_all, is_page, fname, risk_level)
        yield ev("confirm", reason=reason, skill="agent_delete", risk_level=risk_level,
                 target="all" if is_delete_all else ("page" if is_page else "file"))
        return

    # ── 已确认 → 执行删除(单进程合并: 直接调业务层删除逻辑, 不经 http 回环) ──
    from ...db import SessionLocal
    from ...repos.business_repos import artifact_repo, conv_repo
    from ...cache import cache_delete

    try:
        async with SessionLocal() as db:
            if is_delete_all:
                deleted = await artifact_repo.delete_all(db, project_id=project_id)
                msg_text = f"已成功删除全部 {deleted} 个生成产物。如需重新生成网站，随时告诉我～"
            elif is_page:
                # 删页面/模块: 当前建站产物按整站交付, 精确删页需重建, 标记为待办(不物理删)
                logger.info("[agent_delete] 页面删除→标记待办 trace=%s target=%s", trace_id, user_msg[:30])
                yield ev("token", data=f"「{user_msg[:30]}」属于整站产物的一部份。已为你标记为待办："
                                     "重建站点时会排除该页面。或者你也可以直接说『删除所有产物』后重新建站～")
                yield ev("node", stage="done")
                return
            elif fname:
                deleted = await artifact_repo.delete_file(db, project_id=project_id, filename=fname)
                if not deleted:
                    yield ev("token", data=f"未找到文件「{fname}」，无需删除。")
                    yield ev("node", stage="done")
                    return
                msg_text = f"已删除文件「{fname}」。"
            else:
                yield ev("token", data="未能确定要删除的目标，请换个方式说（如「删除index.html」或「删除所有产物」）。")
                return
            # 清除 site_generated 缓存, 防止删除后 cascade 仍认为站点已生成
            conversations = await conv_repo.list_by(db, project_id=project_id)
            for c in conversations:
                await cache_delete(f"site_generated:{c.id}")
            logger.info("[agent_delete] 删除成功 trace=%s deleted=%s", trace_id, deleted)
        yield ev("token", data=msg_text)
        yield ev("node", stage="done")
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
