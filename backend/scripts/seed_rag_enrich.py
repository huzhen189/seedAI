"""丰富向量知识底座：向 intents / components / kb_design 补充种子数据。

可重复运行（upsert 幂等，id 含内容 hash，同内容只更新不新增）。
意图集合 intents 已并入原 kb_intent，是唯一的意图语义集合。

用法（项目 venv）：
    python scripts/seed_rag_enrich.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许以脚本方式运行：把 backend 加入 sys.path。
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.ragstore import upsert  # noqa: E402

# ---------------------------------------------------------------- intents 示例
# (用户原话, 意图 id)。覆盖建站/改站/研究/项目操作/闲聊，扩展语义覆盖面。
INTENT_EXAMPLES: list[tuple[str, str]] = [
    ("帮我做一个个人作品集网站", "site_create"),
    ("生成一份企业官网", "site_create"),
    ("搭建一个电商落地页", "site_create"),
    ("做一个博客站点", "site_create"),
    ("给我建个摄影作品集", "site_create"),
    ("创建一个 SaaS 产品页", "site_create"),
    ("帮我设计个活动报名页", "site_create"),
    ("做个餐厅官网", "site_create"),
    ("把首页改成深色风格", "site_edit"),
    ("加一个客户评价板块", "site_edit"),
    ("联系我们板块换成表单", "site_edit"),
    ("在关于页加团队成员", "site_edit"),
    ("把导航栏改成居中", "site_edit"),
    ("首页标题改大一点", "site_edit"),
    ("加个价格方案板块", "site_edit"),
    ("把配色换成蓝色系", "site_edit"),
    ("帮我调研一下2026年网页设计趋势", "research"),
    ("搜索竞品官网的设计亮点", "research"),
    ("查一下响应式布局最佳实践", "research"),
    ("删除我的旧官网", "project_trash"),
    ("把那个测试项目回收站里删掉", "project_trash"),
    ("移除项目A", "project_trash"),
    ("把回收站里的官网恢复回来", "project_restore"),
    ("还原我刚删的项目", "project_restore"),
    ("把作品集发布上线", "project_publish"),
    ("部署这个官网到生产", "project_publish"),
    ("今天天气怎么样", "chat_ask"),
    ("你好，在吗", "chat_ask"),
    ("解释一下什么是向量数据库", "chat_ask"),
    ("AI 生成网站的原理是什么", "chat_ask"),
]

# ---------------------------------------------------------------- components 片段
# 受信任的 curated HTML 片段（不含 iframe/object/embed/javascript:/onerror/onload，
# 可被 produce 的"组件灵感"区块安全内联展示）。使用站点设计令牌 var(--*)。
COMPONENT_SNIPPETS: list[tuple[str, str, str]] = [
    ("pricing", "commerce",
     '<div class="price-card glass"><div class="price-name">专业版</div>'
     '<div class="price-num">¥199<span>/月</span></div>'
     '<ul class="price-feats"><li>无限项目</li><li>优先支持</li><li>自定义域名</li></ul>'
     '<a class="btn primary" href="#">选择套餐</a></div>'),
    ("testimonial", "corporate",
     '<blockquote class="quote glass"><p>“交付速度超出预期，设计质感也很在线。”</p>'
     '<footer>— 某科技公司 产品负责人</footer></blockquote>'),
    ("cta", "landing",
     '<div class="cta-band glass"><h3>准备好开始了吗？</h3>'
     '<p>三分钟对话，拥有一个可上线的站点。</p>'
     '<a class="btn primary" href="#">免费创建</a></div>'),
    ("stats", "corporate",
     '<div class="stats-row">'
     '<div class="stat"><div class="stat-num">12k+</div><div class="stat-label">活跃项目</div></div>'
     '<div class="stat"><div class="stat-num">99.9%</div><div class="stat-label">可用性</div></div>'
     '<div class="stat"><div class="stat-num">4.9</div><div class="stat-label">用户评分</div></div></div>'),
    ("feature-grid", "portfolio",
     '<div class="feat-grid">'
     '<article class="card glass"><h3>极速生成</h3><p>对话即出稿，无需手写代码。</p></article>'
     '<article class="card glass"><h3>质感在线</h3><p>玻璃拟态与流畅动效开箱即用。</p></article>'
     '<article class="card glass"><h3>随时迭代</h3><p>一句话改主题、加板块。</p></article></div>'),
    ("nav-center", "corporate",
     '<nav class="nav-center glass"><a href="#home">首页</a><a href="#about">关于</a>'
     '<a href="#works">作品</a><a href="#contact">联系</a></nav>'),
    ("footer", "landing",
     '<footer class="foot glass"><span>© 2026 你的品牌</span>'
     '<span>隐私 · 条款 · 联系我们</span></footer>'),
    ("hero-accent", "landing",
     '<section class="hero"><h1>为增长而设计</h1>'
     '<p class="lead">把访客变成客户，从第一屏开始。</p>'
     '<a class="btn primary" href="#">立即体验</a></section>'),
    ("badge", "landing",
     '<span class="badge">全新上线</span>'),
    ("avatar-row", "portfolio",
     '<div class="avatars"><span class="avatar">A</span><span class="avatar">B</span>'
     '<span class="avatar">C</span><span class="avatar more">+9</span></div>'),
    ("timeline", "corporate",
     '<ol class="timeline"><li><b>需求</b>对话澄清目标</li>'
     '<li><b>生成</b>产出首版站点</li><li><b>上线</b>一键发布</li></ol>'),
    ("faq", "blog",
     '<details class="faq glass"><summary>支持自定义域名吗？</summary>'
     '<p>支持，专业版可绑定你自己的域名。</p></details>'),
    ("gallery", "portfolio",
     '<div class="gallery">'
     '<div class="g-item" style="background:linear-gradient(135deg,var(--accent),var(--accent-2))"></div>'
     '<div class="g-item" style="background:linear-gradient(135deg,var(--accent-2),var(--accent))"></div>'
     '<div class="g-item" style="background:color-mix(in srgb,var(--accent) 60%,#fff)"></div></div>'),
    ("newsletter", "blog",
     '<form class="news glass"><input type="email" placeholder="你的邮箱" aria-label="邮箱">'
     '<button class="btn primary" type="submit">订阅</button></form>'),
    ("logos", "corporate",
     '<div class="logos"><span>ACME</span><span>GLOBEX</span><span>INITECH</span><span>UMBRELLA</span></div>'),
]

# ---------------------------------------------------------------- kb_design 设计原则
# 每条带 accent / accent2 元数据，produce 的 _accent_override 可据此微调主题色板。
KB_DESIGN: list[tuple[str, str, str]] = [
    ("科技感站点宜用冷色强调色（靛蓝/青绿）与大留白，传递专业与前沿。", "#6d5efc", "#19c2a3"),
    ("作品集宜突出视觉作品，用大图网格与克制文字，强调色仅点缀。", "#ff7a59", "#ffb259"),
    ("企业官网需稳定可信，主色用深蓝/墨绿，避免高饱和刺眼配色。", "#2b5cff", "#1f9e8a"),
    ("博客/内容站重阅读，正文行高≥1.7，强调色仅用于链接与重点。", "#7c5cff", "#3ec6a8"),
]


async def main() -> None:
    print(f"Chroma 目标: {settings.chroma_url}")
    c_intents = settings.chroma_collection_intents
    c_components = settings.chroma_collection_components
    c_kb = settings.chroma_collection_kb_design

    n_i = await upsert(
        c_intents,
        [t for t, _ in INTENT_EXAMPLES],
        metadatas=[{"kind": "example", "intent_id": iid, "source": "seed"} for _, iid in INTENT_EXAMPLES],
        id_prefix="seed",
    )
    print(f"[intents] 写入/更新 {n_i} 条")

    n_c = await upsert(
        c_components,
        [html for _, _, html in COMPONENT_SNIPPETS],
        metadatas=[{"kind": "component", "type": t, "theme": th} for t, th, _ in COMPONENT_SNIPPETS],
        id_prefix="seed",
    )
    print(f"[components] 写入/更新 {n_c} 条")

    n_k = await upsert(
        c_kb,
        [text for text, _, _ in KB_DESIGN],
        metadatas=[{"kind": "design", "accent": a, "accent2": a2} for _, a, a2 in KB_DESIGN],
        id_prefix="seed",
    )
    print(f"[kb_design] 写入/更新 {n_k} 条")

    total = n_i + n_c + n_k
    print(f"完成，共 {total} 条（幂等：重复运行仅更新不新增）。")


if __name__ == "__main__":
    asyncio.run(main())
