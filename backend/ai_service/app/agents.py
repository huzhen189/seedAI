"""Agent 注册表: 所有 Agent 的 id/名称/头像/描述。
由 /agents 端点供前端加载, SSE 只需传 agent_id, 前端 lookup 渲染。
"""

AGENTS = [
    {
        "id": "explain",
        "name": "小胡",
        "avatar": "🤖",
        "role": "智能助手",
        "description": "回答技术问题、解释概念、闲聊引导需求",
    },
    {
        "id": "build_agent",
        "name": "建站小胡(旧)",
        "avatar": "🏗️",
        "role": "建站专家(已废弃)",
        "description": "旧版建站Agent，已被requirement+builder替代",
    },
    {
        "id": "requirement_agent",
        "name": "需求小胡",
        "avatar": "📋",
        "role": "需求分析师",
        "description": "深度对话收集需求、出方案选项、输出需求文档",
    },
    {
        "id": "builder_agent",
        "name": "建站小胡",
        "avatar": "⚡",
        "role": "代码生成",
        "description": "基于需求文档生成/修改单文件HTML",
    },
    {
        "id": "design_agent",
        "name": "设计小胡",
        "avatar": "🎨",
        "role": "设计顾问",
        "description": "配色方案、布局建议、字体推荐、动效设计",
    },
    {
        "id": "fix_agent",
        "name": "修复小胡",
        "avatar": "🔧",
        "role": "BUG修复",
        "description": "错误排查、代码修复、直接给出修复方案",
    },
    {
        "id": "review_agent",
        "name": "评审小胡",
        "avatar": "🔍",
        "role": "代码评审",
        "description": "性能/SEO/可访问性/代码质量评审",
    },
    {
        "id": "write_code",
        "name": "编码小胡",
        "avatar": "💻",
        "role": "代码编写",
        "description": "写HTML/CSS/JS函数片段、UI组件、Bug修复",
    },
    {
        "id": "generate_doc",
        "name": "文档小胡",
        "avatar": "📄",
        "role": "文档生成",
        "description": "写README、教程、方案设计文档",
    },
    {
        "id": "build_agent_coder",
        "name": "建站小胡",
        "avatar": "⚡",
        "role": "代码生成(内部)",
        "description": "建站流程的代码生成阶段(内部调用)",
    },
]

# 名称→对象快速查找
BY_ID = {a["id"]: a for a in AGENTS}


def get_agent(agent_id: str) -> dict | None:
    return BY_ID.get(agent_id)
