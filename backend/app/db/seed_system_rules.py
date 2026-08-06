"""系统规则种子数据 + 幂等重插（reset 后由 reset_all 调用，保证刚性规则不被清空）。

双轨写入：
  1) MySQL(SoT)：按 rule_key 幂等 UPSERT；仅当内容/属性真变更时才 version+1（审计/回滚友好）。
  2) 向量库：seed 完成后用最新 MySQL 行重建 system_rules 集合（clear + upsert 活跃规则）。

CANONICAL_RULES 是一组**模拟规则原文**，覆盖 global/domain/user/project × constraint/
guardrail/policy/preference 的完整分类，用于验证双轨链路真实读写。user:demo / project:demo
是示例作用域，仅当会话 scope 含对应 token 时才召回（见 services/system_rules.scope_key_of）。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import transaction
from app.models import SystemRule
from app.services.system_rules import rebuild_vector_collection

logger = logging.getLogger("app.db.seed_system_rules")

# 模拟规则原文（summary+keywords 供向量嵌入；content 为注入 Prompt 的完整文本）。
CANONICAL_RULES: list[dict] = [
    # ── 全局硬约束 constraint ──
    {
        "rule_key": "global.no_secret_leak",
        "scope": "global", "scope_ref": None,
        "rule_type": "constraint", "priority": 90,
        "title": "禁止泄露密钥",
        "content": "严禁在回复、生成的代码或配置中输出任何真实密钥、密码、Token、API Key、"
                   "数据库连接串等敏感凭证。如需示意，一律使用明显占位符（如 <YOUR_API_KEY>）。",
        "summary": "不在任何输出中泄露真实密钥密码Token等敏感凭证，用占位符代替",
        "keywords": "密钥|密码|token|apikey|凭证|敏感|泄露|占位符",
    },
    {
        "rule_key": "global.no_destructive_cmd",
        "scope": "global", "scope_ref": None,
        "rule_type": "constraint", "priority": 90,
        "title": "禁止破坏性命令",
        "content": "禁止执行或建议执行破坏性系统命令，包括但不限于 rm -rf、格式化磁盘、"
                   "DROP DATABASE、DELETE 不带 WHERE 的清空、修改系统引导等。涉及危险操作必须先警告并征求确认。",
        "summary": "不执行或建议rm -rf、DROP DATABASE等破坏性命令，危险操作先确认",
        "keywords": "rm -rf|drop database|格式化|破坏性|危险命令|删除库",
    },
    {
        "rule_key": "global.no_fabricated_url",
        "scope": "global", "scope_ref": None,
        "rule_type": "constraint", "priority": 80,
        "title": "禁止臆造链接",
        "content": "不臆造任何未经验证的外部 URL、链接、第三方服务名或下载地址。引用资源时必须基于"
                   "用户已提供或公开可核实的信息；不确定的链接要明确说明“需用户自行核实”。",
        "summary": "不编造未经验证的外部链接第三方服务名，引用须可核实",
        "keywords": "url|链接|臆造|第三方|下载地址|编造",
    },
    # ── 全局护栏 guardrail ──
    {
        "rule_key": "global.no_unimplemented_promise",
        "scope": "global", "scope_ref": None,
        "rule_type": "guardrail", "priority": 75,
        "title": "不承诺未实现能力",
        "content": "不承诺本系统当前未实现的能力（如支付、登录鉴权、后端接口、实时数据库）。"
                   "被问及未实现能力时，先一句话说明边界，再给出可行替代方案或可落地的下一步。",
        "summary": "不承诺支付登录后端等未实现能力，被问时给边界与替代方案",
        "keywords": "支付|登录|后端|数据库|未实现|能力边界|替代方案",
    },
    # ── 全局策略 policy ──
    {
        "rule_key": "global.chinese_reply",
        "scope": "global", "scope_ref": None,
        "rule_type": "policy", "priority": 60,
        "title": "默认中文回复",
        "content": "默认使用简体中文回复；用户明确要求其他语言时切换。技术术语可保留英文原文。",
        "summary": "默认简体中文回复，技术术语可保留英文",
        "keywords": "中文|语言|回复|术语",
    },
    # ── 域：chat ──
    {
        "rule_key": "domain:chat.no_long_code",
        "scope": "domain", "scope_ref": "chat",
        "rule_type": "policy", "priority": 55,
        "title": "闲聊不堆代码",
        "content": "在闲聊/澄清意图场景中，不输出整段代码；除非用户明确要求看示例片段，才给最小可运行片段。",
        "summary": "闲聊澄清场景不输出整段代码，除非用户要求示例片段",
        "keywords": "闲聊|代码|示例|澄清",
    },
    # ── 域：site（建站）──
    {
        "rule_key": "domain:site.responsive_required",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "constraint", "priority": 85,
        "title": "网页必须响应式",
        "content": "生成的网页必须响应式、在移动端可用：包含 viewport meta、使用流式布局/"
                   "媒体查询、可点击区域不小于 44px。桌面与移动端均需正常显示。",
        "summary": "生成的网页必须响应式移动端可用含viewport媒体查询",
        "keywords": "响应式|移动端|viewport|媒体查询|自适应|移动优先",
    },
    {
        "rule_key": "domain:site.no_hardcoded_secret",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "constraint", "priority": 88,
        "title": "前端不硬编码密钥",
        "content": "禁止将任何密钥、Token、私有 API Key 硬编码进前端 HTML/JS/CSS。敏感配置使用"
                   "占位符并在说明中提示走环境变量/后端代理，绝不出现在客户端源码。",
        "summary": "不把密钥token硬编码进前端代码，用占位符走环境变量",
        "keywords": "硬编码|前端|密钥|token|环境变量|客户端",
    },
    {
        "rule_key": "domain:site.semantic_html",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "policy", "priority": 60,
        "title": "语义化 HTML",
        "content": "使用语义化 HTML 标签（header/main/footer/nav/section/article 等）组织结构，"
                   "配合 alt 文本与合理标题层级，提升可访问性(可满足 WCAG 2.1 AA 基础项)。",
        "summary": "使用语义化HTML标签提升可访问性WCAG",
        "keywords": "语义化|html|可访问性|a11y|wcag|alt",
    },
    {
        "rule_key": "domain:site.dark_mode_support",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "preference", "priority": 40,
        "title": "默认白色（浅色）模式",
        "content": "默认使用白色/浅色主题：用 CSS 变量定义配色，默认浅色，并尊重 prefers-color-scheme "
                   "自动切换（用户偏好暗色时再切）。",
        "summary": "默认白色浅色主题，CSS变量加prefers-color-scheme自动切换",
        "keywords": "白色|浅色|light|主题|css变量|prefers-color-scheme",
    },
    # ── 域：research ──
    {
        "rule_key": "domain:research.cite_source",
        "scope": "domain", "scope_ref": "research",
        "rule_type": "guardrail", "priority": 70,
        "title": "引用须标注来源",
        "content": "引用外部事实、数据或观点时必须标注来源（出处/作者/时间）；无法核实的引用要明确"
                   "说明“来源待核实”，不得编造参考文献。",
        "summary": "引用外部事实数据须标注来源，不编造参考文献",
        "keywords": "引用|来源|参考文献|出处|核实",
    },
    # ── 用户级示例（仅当会话 scope 含 user:demo 时召回）──
    {
        "rule_key": "user:demo.no_red",
        "scope": "user", "scope_ref": "demo",
        "rule_type": "preference", "priority": 45,
        "title": "示例用户忌用红色",
        "content": "示例用户偏好：主色避免使用红色（配色禁忌示例）。如需强调可用其品牌色或深蓝替代。",
        "summary": "示例用户偏好主色避免红色，用品牌色或深蓝替代",
        "keywords": "红色|配色|禁忌|品牌色|偏好",
    },
    # ── 静态输出约束（修正旧「示例项目用 Vue3+Vite」的错误认知）──
    {
        "rule_key": "global.static_output",
        "scope": "global", "scope_ref": None,
        "rule_type": "policy", "priority": 70,
        "title": "只生成静态自包含网页",
        "content": "agent 只能生成自包含的静态网页：单文件 HTML + 内联 CSS/JS（或少量同源静态资源），"
                   "可直接用浏览器打开或用 iframe 嵌入预览，无需任何构建步骤。Vue3 可以用，但只能用「静态」"
                   "形式——通过 CDN/UMD 等方式在页面里直接引入，单个 HTML 即可运行、无需编译打包；不支持"
                   "需要 npm 构建、Vite 脚手架、起本地端口跑 dev server 的工程化用法（这类产物不是静态文件，"
                   "无法以静态文件直接预览，且当前环境没有配套的构建/预览服务）。",
        "summary": "只生成静态自包含网页；Vue3仅可用CDN静态引入，不支持编译打包跑端口",
        "keywords": "静态网页|单文件HTML|预览|iframe|Vue3|CDN|Vite|构建|端口|无后端",
    },
    # ── 站点数据策略：无后端 → 前端静态模拟；本地库按需升级 ──
    {
        "rule_key": "domain:site.static_data",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "policy", "priority": 62,
        "title": "静态优先·本地库按需升级",
        "content": "生成的站点为纯静态前端，后端能力暂不支持：所有数据交互先在前端用写死的示例数据 / 静态按钮"
                   "模拟，不依赖任何服务端。持久化默认只用静态按钮与内存态；浏览器本地数据库（IndexedDB / "
                   "localStorage）作为升级方案——仅当用户明确确认「升级到使用浏览器本地数据库」时才启用本地库"
                   "读写，否则一律静态模拟。",
        "summary": "站点纯静态无后端，数据先用前端静态模拟，本地库仅用户确认升级后启用",
        "keywords": "静态|无后端|前端模拟|静态按钮|本地数据库|IndexedDB|localStorage|升级",
    },
    # ── 域：site（建站）—— 这几条固化 v2 之后的前置校验与生成基线，避免重蹈空 spec 退化 ──
    {
        "rule_key": "domain:site.prebuild_slot_collection",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "constraint", "priority": 82,
        "title": "建站前必须收齐必填槽位",
        "content": "生成或编辑网站前，必须先通过收集闸门收齐必填槽位（如站点主题 / 类型 / 板块）。即使用户上一轮刚建过站、SIR 状态机出现 edit_mode 信号，也不得跳过收集闸门——create 意图永远强制收槽，edit 意图仍需先确认存在已建成站点。不得退化成空 spec 模板站。",
        "summary": "建站前必收齐必填槽位，create永远强制收槽，edit须先确认站存在",
        "keywords": "必填槽位|收集闸门|edit_mode|空spec|模板站|收槽",
    },
    {
        "rule_key": "domain:site.targeted_action_precheck",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "constraint", "priority": 86,
        "title": "指向既有资源的意图须先校验目标就绪",
        "content": "对用户既有资源的操作（site 的 edit/review，project 的 publish/trash/restore/purge）必须在 S5 闸门、执行前校验目标真实存在且就绪：site 类需存在 status 为 verified 或 preview_ready 的已建成站点，否则 S5 直接打回并提示先新建或切换项目；project 类需目标项目存在且未被永久删除（purging）。前置条件不满足不允许进入执行或审批落地。",
        "summary": "edit/review/publish/trash须先校验目标就绪，否则S5打回",
        "keywords": "前置校验|打回|edit|review|publish|目标不存在|站点未建成|purging",
    },
    {
        "rule_key": "domain:site.deterministic_baseline_with_rag",
        "scope": "domain", "scope_ref": "site",
        "rule_type": "policy", "priority": 61,
        "title": "确定性模板为基线，RAG 仅增强不替换",
        "content": "站点 HTML 生成以确定性模板为基线（标题 / 主题 / 板块经转义、无外部 CDN），RAG 检索（设计原则 / 组件灵感）只做增强、绝不替换基线；任一检索失败静默跳过（fail-soft），不改变确定性产出。修复轮（repair_round）跳过一切 RAG 增强，仅做确定性基线并在末尾统一 sanitize，防止危险片段注入。",
        "summary": "站点生成以确定性模板为基线，RAG仅增强不替换，修复轮跳过增强",
        "keywords": "确定性|模板|RAG|增强不替换|修复轮|sanitize|fail-soft",
    },
    # ── 全局策略：组件库知识底座治理（v2 已关停自增强回写）──
    {
        "rule_key": "global.no_component_autofeed",
        "scope": "global", "scope_ref": None,
        "rule_type": "policy", "priority": 58,
        "title": "组件库停止自动回写",
        "content": "系统组件库（RAG components 集合）仅由管理员维护的 curated 种子构成，不再把每次建站产物自动回写进组件库。禁止任何「建站后抽取片段喂回组件库」的自增强闭环（会造成跨用户 / 跨项目语义污染、集合无上限膨胀、维护成本高）。建站时的「组件灵感」区块只从 curated 种子召回。",
        "summary": "组件库只保留curated种子，禁止建站产物自动回写避免污染膨胀",
        "keywords": "组件库|自增强|回写|自动喂养|curated|污染|膨胀",
    },
]


async def seed_system_rules(*, rebuild_vector: bool = True) -> int:
    """幂等重插系统规则（MySQL 真相 + 向量索引）。返回活跃规则条数。

    - 已存在且内容未变 → 不动（version 不变，审计稳定）。
    - 已存在但内容/属性变更 → 更新并 version+1（可追溯每次修订）。
    - 不存在 → 新增 version=1。
    整库重置后调用，保证刚性规则随 schema 重建而回归；若向量不可达，MySQL 仍落库成功。
    """
    async with transaction() as session:
        inserted = 0
        updated = 0
        for spec in CANONICAL_RULES:
            existing = (
                await session.execute(
                    select(SystemRule).where(SystemRule.rule_key == spec["rule_key"])
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    SystemRule(
                        rule_key=spec["rule_key"],
                        scope=spec["scope"],
                        scope_ref=spec.get("scope_ref"),
                        rule_type=spec["rule_type"],
                        title=spec["title"],
                        content=spec["content"],
                        summary=spec["summary"],
                        keywords=spec["keywords"],
                        priority=spec.get("priority", 50),
                        version=1,
                        is_active=True,
                    )
                )
                inserted += 1
                continue
            changed = (
                existing.content != spec["content"]
                or existing.title != spec["title"]
                or existing.summary != spec["summary"]
                or existing.keywords != spec["keywords"]
                or existing.scope != spec["scope"]
                or existing.scope_ref != spec.get("scope_ref")
                or existing.rule_type != spec["rule_type"]
                or existing.priority != spec.get("priority", 50)
            )
            existing.scope = spec["scope"]
            existing.scope_ref = spec.get("scope_ref")
            existing.rule_type = spec["rule_type"]
            existing.title = spec["title"]
            existing.content = spec["content"]
            existing.summary = spec["summary"]
            existing.keywords = spec["keywords"]
            existing.priority = spec.get("priority", 50)
            existing.is_active = True
            if changed:
                existing.version = (existing.version or 1) + 1
                updated += 1
        logger.info(
            "[seed_system_rules] 比对完成: 新增 %d 条, 内容变更更新 %d 条, 共 %d 条候选",
            inserted, updated, len(CANONICAL_RULES),
        )
        await session.flush()
        rows = (
            await session.execute(select(SystemRule).where(SystemRule.is_active.is_(True)))
        ).scalars().all()
        rule_dicts = [
            {
                "id": r.id,
                "rule_key": r.rule_key,
                "scope": r.scope,
                "scope_ref": r.scope_ref,
                "rule_type": r.rule_type,
                "title": r.title,
                "summary": r.summary,
                "keywords": r.keywords,
                "priority": r.priority or 50,
                "is_active": r.is_active,
            }
            for r in rows
        ]
    # 事务已提交；重建向量集合（fail-soft，向量不可达不影响 MySQL 真相）。
    if rebuild_vector:
        seeded = await rebuild_vector_collection(rule_dicts)
        logger.info("[seed_system_rules] MySQL 落库 %d 条，向量重建 %d 条", len(rule_dicts), seeded)
    else:
        logger.info("[seed_system_rules] MySQL 落库 %d 条（跳过向量重建）", len(rule_dicts))
    return len(rule_dicts)


__all__ = ["CANONICAL_RULES", "seed_system_rules"]
