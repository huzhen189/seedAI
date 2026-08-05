"""SiteWorkflow：Spec → Produce/Edit → Verify → Preview。

这是规范 §8.2 的固定子流程。当前在没有外部 LLM 依赖的前提下，
从 ``projects.site_spec``（需求文档）确定性地生成一份**真实可用、premium 质感**
的静态站点：语义化结构、暗/亮/跟随系统三态主题、玻璃拟态、响应式、滚动渐显、
无任何外部 CDN 请求（vendor 全部内联，满足 §11.3 的隔离与确定性）。

Verify 负责结构完整性与注入安全；Produce 默认走"生成完整版本"，
修改时基于上一稳定版本做受控覆盖（不原地破坏 active 版本）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from html import escape
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.turn_context import TurnContext
from app.models import Artifact, Project
from app.ragstore import retrieve as _rag_retrieve, safe_upsert_bg as _rag_upsert_bg

import logging

logger = logging.getLogger("app.site.workflow")

# 句子切分：用于从用户 prompt 中拆出「要点」作为 feature card 标题/正文。
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;~\n]+")
# 词切分：用于从句子首部提取卡片标题（取第一个词，截断到 10 字）。
_WORD_SPLIT = re.compile(r"[，,、\s]+")

# 规范 section key → (卡片标题, 描述)。与 router/intent.py 的 _SECTION_MAP 规范值一一对应，
# 让 DST 沉淀的板块槽位在产物上真实可见（而不是只停留在 spec JSON 里）。
_SECTION_LABELS: dict[str, tuple[str, str]] = {
    "contact": ("联系我", "留下联系方式与合作入口，方便访客直接找到你。"),
    "testimonials": ("客户评价", "展示真实客户反馈，建立信任与口碑背书。"),
    "about": ("关于", "介绍背景、理念与专业能力。"),
    "gallery": ("作品展示", "精选案例与作品，用视觉说话。"),
    "pricing": ("价格方案", "清晰的套餐与定价，降低决策成本。"),
    "blog": ("博客文章", "持续输出内容，沉淀专业影响力。"),
    "team": ("团队成员", "介绍核心成员与分工。"),
    "faq": ("常见问题", "提前解答高频疑问，减少沟通成本。"),
}


def _esc(value: str) -> str:
    """HTML 正文转义。所有用户内容都经过此处,构造上杜绝 <script> 注入。

    使用标准库 ``html.escape(quote=True)``,既转义 ``&<>`` 也转义引号,
    保证内容无论落在标签体还是属性值里都不会破坏结构或被当成标签解析。
    """
    return escape(str(value), quote=True)


def _sentences(prompt: str) -> list[str]:
    """把一段自由文本按中英文标点切成句子列表,丢弃空串。

    Args:
        prompt: 用户输入的原始/清洗后文本。
    Returns:
        非空句子列表(已 strip)。空文本返回 ``[]``。
    """
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(prompt or "")]
    return [p for p in parts if p]


def _truncate(value: str, limit: int) -> str:
    """超长截断到 ``limit`` 字符(留一位放省略号)。长度不足原样返回。"""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _verify_html(html: str) -> tuple[bool, str]:
    """结构完整性与注入安全校验（规范 §8.2 Verify）。

    模块级函数，供 ``HtmlValidateTool`` 复用，避免逻辑漂移。

    校验项（任一失败即返回 ``(False, reason)``）：
      1. 必须非空且以 ``<!doctype html>`` 开头；
      2. 必须含闭合 ``</html>``；
      3. 字节长度 >= 400（防止半成品/占位页）；
      4. 不得含危险标签/属性(``iframe/object/embed/javascript:/onerror=/onload=``),
         作为转义之后的最后一道注入闸门。

    Args:
        html: 待校验的完整 HTML 文档字符串。
    Returns:
        ``(passed, reason)`` —— 通过时 ``reason == "ok"``。
    """
    if not html or not html.strip().lower().startswith("<!doctype html>"):
        logger.warning("[verify] 校验失败: 缺少 <!doctype html> 或为空")
        return False, "site_verify_missing_doctype"
    if "</html>" not in html.lower():
        logger.warning("[verify] 校验失败: 缺少 </html> 闭合标签")
        return False, "site_verify_unclosed_html"
    if len(html.encode("utf-8")) < 400:
        logger.warning("[verify] 校验失败: 产物过小 (%d bytes < 400)", len(html.encode("utf-8")))
        return False, "site_verify_too_small"
    # 危险标签/属性不得在生成产物中出现（用户内容已全量转义，这里做最后一道闸门）。
    lowered = html.lower()
    for forbidden in ("<iframe", "<object", "<embed", "javascript:", "onerror=", "onload="):
        if forbidden in lowered:
            logger.warning("[verify] 校验失败: 检出危险 token=%s", forbidden)
            return False, "site_verify_unsafe_token"
    logger.debug("[verify] 校验通过: %d bytes", len(html.encode("utf-8")))
    return True, "ok"


# ----------------------------------------------------------------- 修复轮后处理
_DANGEROUS_TAGS = ("iframe", "object", "embed")
_DANGEROUS_SUBSTRINGS = ("javascript:", "onerror=", "onload=")


def _sanitize_html(html: str) -> str:
    """修复轮确定性后处理：移除注入风险 token 并兜底结构完整性。

    仅在校验未过时由 ``produce`` 的 repair 轮(``_repair_round``)调用，
    使下一轮产物与首轮不同（去掉可能含危险片段的 RAG 增强），从而打破空转。
    """
    out = html
    for tag in _DANGEROUS_TAGS:
        out = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", out, flags=re.I | re.S)
        out = re.sub(rf"<{tag}\b[^>]*/>", "", out, flags=re.I)
    for tok in _DANGEROUS_SUBSTRINGS:
        out = out.replace(tok, "")
    if not out.strip().lower().startswith("<!doctype html>"):
        out = "<!doctype html>\n" + out
    if "</html>" not in out.lower():
        out = out.rstrip() + "\n</html>"
    return out


# ----------------------------------------------------------------- RAG 增强辅助
def _render_components_section(hits) -> str:
    """把检索到的组件库片段渲染成「组件灵感」区块（curated 片段，非用户内容，安全）。

    每个命中是受信任的种子 HTML 片段，直接内联展示；附带作用域内的组件网格样式。
    """
    cards = []
    for h in (hits or []):
        snippet = (h.text or "").strip()
        if not snippet:
            continue
        cards.append(f'        <div class="comp-glass glass reveal">{snippet}</div>')
    if not cards:
        return ""
    return (
        '<section class="block" id="components">\n'
        '  <div class="wrap">\n'
        '    <h2 class="section-head reveal">组件灵感</h2>\n'
        '    <p class="section-sub reveal">来自组件库的精选模块，可随时对话替换或调整。</p>\n'
        '    <style>\n'
        '      .comp-grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));}\n'
        '      .comp-glass{padding:22px;overflow:hidden;}\n'
        '      .comp-glass :is(h1,h2,h3){margin-top:0;letter-spacing:-.02em;}\n'
        '    </style>\n'
        '    <div class="comp-grid">\n'
        + "\n".join(cards)
        + '\n    </div>\n  </div>\n</section>'
    )


def _accent_override(hits) -> str:
    """若设计原则命中带 accent 元数据，则微调主题色板（轻量、可逆）。"""
    for h in (hits or []):
        m = h.metadata or {}
        accent = m.get("accent")
        if not accent:
            continue
        accent2 = m.get("accent2") or accent
        return f"<style>:root{{--accent:{accent};--accent-2:{accent2};}}</style>"
    return ""


def _check_error_patterns(html: str, patterns) -> None:
    """历史错误模式守卫（advisory，非致命）：检出已知坑则记日志，不阻断产出。"""
    if not html or not patterns:
        return
    lowered = html.lower()
    for p in patterns:
        token = (p or "").strip().lower()
        if token and token in lowered:
            logger.warning("[produce] 检出历史错误模式签名: %s", token[:60])


class SiteWorkflow:
    # ---------------------------------------------------------- Spec
    async def build_spec(
        self, session: AsyncSession, project: Project, context: TurnContext,
        scoped_slots: dict | None = None,
    ) -> dict:
        """把本轮明确指令 + **DST 合并后的 SIR 槽位** 合并进 ``projects.site_spec``。

        关键：spec 的真相来源是 ``context.sir_after_dst.slots``（S3 合并 base+delta 的结果），
        而不是单轮的 ``clean_message``。这样"第一轮说深色作品集 → 第二轮只说改成浅色"时，
        `site.type=portfolio` 会从基态保留，只有 `site.theme` 被本轮覆盖——即真正的
        增量改站，而非每轮从零重解析（后者正是"改一句就丢掉之前所有需求"的根因）。

        槽位映射：
          - ``site.theme`` → ``spec.theme``（单值覆盖）
          - ``site.type``  → ``spec.site_type``（单值覆盖）
          - ``site.sections`` → ``spec.sections``（并集累积，改站只增不抹）
          - ``site.style`` → ``spec.styles``（并集累积）
          - ``site.brief`` → ``spec.brief``（站点主体描述）

        Args:
            session: 数据库会话(本方法只改 ``project.site_spec`` 内存对象,提交由调用方控制)。
            project: 当前项目(``project.site_spec`` 是既有需求 JSON)。
            context: 本轮 TurnContext。
        Returns:
            合并后的 spec dict(也会写回 ``project.site_spec``)。
        """
        logger.debug("[build_spec] project=%s 合并指令: %.80s", project.id, context.clean_message)
        spec = dict(project.site_spec or {})
        spec["title"] = project.name
        spec["prompt"] = context.clean_message
        # 累积需求历史，便于后续 edit 基于既有意图做受控 patch。
        history = list(spec.get("history", []))
        history.append(context.clean_message)
        spec["history"] = history[-10:]

        # 作用域隔离：优先用调用方传入的 scoped_slots（只含本子任务相关槽位），
        # 否则回退全量槽位。避免把整轮无关子任务的槽位塞入，防污染。
        slots = dict(scoped_slots) if scoped_slots is not None else dict(context.sir_after_dst.slots or {})
        theme = slots.get("site.theme")
        if isinstance(theme, str) and theme:
            spec["theme"] = theme
        spec.setdefault("theme", "system")

        site_type = slots.get("site.type")
        if isinstance(site_type, str) and site_type:
            spec["site_type"] = site_type

        brief = slots.get("site.brief")
        if isinstance(brief, str) and brief:
            spec["brief"] = brief

        # 多值槽位：并集累积，保序去重。改站说"加个联系我"不能抹掉已有的其它板块。
        for slot_key, spec_key in (("site.sections", "sections"), ("site.style", "styles")):
            incoming = slots.get(slot_key)
            if not isinstance(incoming, list) or not incoming:
                continue
            merged = list(spec.get(spec_key) or [])
            for value in incoming:
                if isinstance(value, str) and value and value not in merged:
                    merged.append(value)
            spec[spec_key] = merged

        project.site_spec = spec
        logger.info(
            "[build_spec] project=%s history=%d theme=%s type=%s sections=%s styles=%s",
            project.id, len(history), spec.get("theme"), spec.get("site_type"),
            spec.get("sections"), spec.get("styles"),
        )
        return spec

    # ---------------------------------------------------------- Produce
    async def produce(self, spec: dict) -> str:
        """按 SiteSpec 确定性生成一份 premium 质感的完整静态站点 HTML（§8.2 Produce）。

        纯函数(无副作用)，从 ``title/prompt/theme`` 推导 hero/feature/about 区块，
        全程经 ``_esc`` 转义，无任何外部 CDN(满足 §11.3 隔离与确定性)。
        额外做 **RAG 增强（fail-soft）**：检索组件库 / 设计原则作为产出补充，
        只增强不替换 —— 组件以「组件灵感」区块展示，设计原则可微调主题色板；
        任一检索失败都静默跳过，绝不改变确定性基线。

        Args:
            spec: SiteSpec dict(至少含 ``title/prompt/theme``)。
        Returns:
            完整 HTML 文档字符串(以 ``<!doctype html>`` 开头)。
        """
        title = _esc(spec.get("title") or "我的网站")
        prompt = spec.get("prompt") or ""
        # 修复轮：读取上轮校验原因，进入确定性修复模式（去 RAG 增强 + 末尾 sanitize）。
        repair_round = spec.get("_repair_round")
        theme = spec.get("theme", "system")
        # 内容源优先取 brief（DST 沉淀的站点主体），其次退回本轮 prompt。
        brief = str(spec.get("brief") or "") or prompt
        sentences = _sentences(brief)
        sections: list[str] = [s for s in (spec.get("sections") or []) if isinstance(s, str)]
        styles: list[str] = [s for s in (spec.get("styles") or []) if isinstance(s, str)]
        logger.debug(
            "[produce] title=%s theme=%s type=%s sections=%s styles=%s 句子数=%d",
            title, theme, spec.get("site_type"), sections, styles, len(sentences),
        )

        hero_subtitle = _esc(sentences[0]) if sentences else _esc("用对话创造的数字体验")
        about_source = " ".join(str(h) for h in (spec.get("history") or [])[-3:]) or brief
        about_text = _esc(about_source) if about_source else _esc("这是一个由 SeedAI 通过对话生成的站点。")

        features = self._derive_features(sentences, sections, styles)
        feature_cards = "\n".join(
            f"""        <article class="card glass reveal">
          <div class="card-dot"></div>
          <h3>{_esc(f['title'])}</h3>
          <p>{_esc(f['desc'])}</p>
        </article>""" for f in features
        )

        # RAG 增强（fail-soft）：检索组件库与设计原则，作为确定性产出的补充。
        # 组件以「组件灵感」区块展示；设计原则可微调主题色板；任一失败静默跳过。
        if repair_round:
            # 修复轮：跳过 RAG 增强（可能携带危险片段），仅做确定性基线，末尾统一 sanitize。
            comp_hits = []
            kb_hits = []
        else:
            comp_hits = await _rag_retrieve(
                settings.chroma_collection_components,
                f"{title} {spec.get('site_type', '')} {' '.join(sections)}",
                top_k=3,
            )
            kb_hits = await _rag_retrieve(
                settings.chroma_collection_kb_design,
                f"{title} {' '.join(styles)}",
                top_k=2,
            )
        comp_section = _render_components_section(comp_hits)
        accent_override = _accent_override(kb_hits)

        html = f"""<!doctype html>
<html lang="zh-CN" data-theme="{_esc(theme)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg:#f6f7f4; --bg-soft:#ffffff; --fg:#181815; --fg-soft:#5b5b54;
  --accent:#6d5efc; --accent-2:#19c2a3; --line:rgba(20,20,20,.08);
  --glass:rgba(255,255,255,.55); --shadow:0 24px 60px -28px rgba(20,20,40,.35);
}}
[data-theme="dark"] {{
  --bg:#0c0d12; --bg-soft:#15171f; --fg:#f3f4f1; --fg-soft:#a3a49c;
  --accent:#8b7dff; --accent-2:#2fe0bd; --line:rgba(255,255,255,.10);
  --glass:rgba(28,30,40,.55); --shadow:0 24px 70px -30px rgba(0,0,0,.7);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]):not([data-theme="dark"]) {{
    --bg:#0c0d12; --bg-soft:#15171f; --fg:#f3f4f1; --fg-soft:#a3a49c;
    --accent:#8b7dff; --accent-2:#2fe0bd; --line:rgba(255,255,255,.10);
    --glass:rgba(28,30,40,.55); --shadow:0 24px 70px -30px rgba(0,0,0,.7);
  }}
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; }}
body {{
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  background:
    radial-gradient(1200px 600px at 12% -8%, color-mix(in srgb, var(--accent) 22%, transparent), transparent 60%),
    radial-gradient(1000px 520px at 100% 0%, color-mix(in srgb, var(--accent-2) 18%, transparent), transparent 55%),
    var(--bg);
  color:var(--fg); min-height:100vh; line-height:1.65; -webkit-font-smoothing:antialiased;
  transition:background .5s ease, color .35s ease;
}}
.wrap {{ max-width:1080px; margin:0 auto; padding:0 24px; }}
.glass {{ background:var(--glass); backdrop-filter:blur(22px) saturate(160%); -webkit-backdrop-filter:blur(22px) saturate(160%); border:1px solid var(--line); border-radius:22px; box-shadow:var(--shadow); }}
a {{ color:var(--accent); text-decoration:none; }}
header.nav {{ position:sticky; top:0; z-index:20; backdrop-filter:blur(14px); border-bottom:1px solid var(--line); }}
.nav .wrap {{ display:flex; align-items:center; justify-content:space-between; height:68px; }}
.brand {{ font-weight:750; letter-spacing:-.02em; font-size:1.15rem; }}
.theme-btn {{ cursor:pointer; border:1px solid var(--line); background:var(--bg-soft); color:var(--fg); border-radius:999px; padding:8px 14px; font-size:.9rem; transition:transform .25s cubic-bezier(.16,1,.3,1), border-color .25s; }}
.theme-btn:hover {{ transform:translateY(-2px); border-color:var(--accent); }}
.hero {{ padding:clamp(64px,12vw,140px) 0 clamp(40px,7vw,80px); }}
.eyebrow {{ display:inline-block; font-size:.8rem; letter-spacing:.18em; text-transform:uppercase; color:var(--accent-2); margin-bottom:18px; }}
.hero h1 {{ font-size:clamp(2.4rem,8vw,5.2rem); line-height:1.02; letter-spacing:-.04em; margin:0 0 22px; }}
.hero h1 .grad {{ background:linear-gradient(100deg,var(--accent),var(--accent-2)); -webkit-background-clip:text; background-clip:text; color:transparent; }}
.hero p.lead {{ font-size:clamp(1.05rem,2.4vw,1.35rem); color:var(--fg-soft); max-width:62ch; margin:0 0 30px; }}
.cta {{ display:inline-flex; gap:12px; flex-wrap:wrap; }}
.btn {{ cursor:pointer; border:none; border-radius:999px; padding:13px 26px; font-size:1rem; font-weight:650; transition:transform .25s cubic-bezier(.16,1,.3,1), box-shadow .25s; }}
.btn.primary {{ background:linear-gradient(100deg,var(--accent),var(--accent-2)); color:#fff; box-shadow:0 16px 40px -16px var(--accent); }}
.btn.primary:hover {{ transform:translateY(-3px); }}
.btn.ghost {{ background:var(--bg-soft); color:var(--fg); border:1px solid var(--line); }}
.btn.ghost:hover {{ transform:translateY(-3px); border-color:var(--accent); }}
section.block {{ padding:clamp(40px,7vw,80px) 0; }}
.section-head {{ font-size:clamp(1.6rem,4vw,2.4rem); letter-spacing:-.03em; margin:0 0 10px; }}
.section-sub {{ color:var(--fg-soft); margin:0 0 30px; max-width:60ch; }}
.grid {{ display:grid; gap:18px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }}
.card {{ padding:26px; position:relative; overflow:hidden; transition:transform .3s cubic-bezier(.16,1,.3,1); }}
.card:hover {{ transform:translateY(-6px); }}
.card-dot {{ width:42px; height:42px; border-radius:13px; margin-bottom:16px; background:linear-gradient(135deg,var(--accent),var(--accent-2)); box-shadow:0 12px 30px -12px var(--accent); }}
.card h3 {{ margin:0 0 8px; font-size:1.2rem; letter-spacing:-.01em; }}
.card p {{ margin:0; color:var(--fg-soft); font-size:.96rem; }}
.about {{ padding:30px clamp(24px,4vw,44px); }}
.about p {{ color:var(--fg-soft); margin:0; }}
footer.foot {{ border-top:1px solid var(--line); margin-top:40px; }}
.foot .wrap {{ display:flex; justify-content:space-between; align-items:center; padding:28px 24px; color:var(--fg-soft); font-size:.85rem; flex-wrap:wrap; gap:10px; }}
.reveal {{ opacity:0; transform:translateY(18px); transition:opacity .7s ease, transform .7s cubic-bezier(.16,1,.3,1); }}
.reveal.in {{ opacity:1; transform:none; }}
@media (prefers-reduced-motion: reduce) {{ .reveal {{ opacity:1; transform:none; transition:none; }} }}
</style>
</head>
<body>
<header class="nav">
  <div class="wrap">
    <div class="brand">{title}</div>
    <button class="theme-btn" id="themeBtn" type="button" aria-label="切换主题">主题</button>
  </div>
</header>

<main>
  <section class="hero">
    <div class="wrap">
      <span class="eyebrow reveal">SeedAI · 对话生成</span>
      <h1 class="reveal">为 <span class="grad">{title}</span><br>打造的数字体验</h1>
      <p class="lead reveal">{hero_subtitle}</p>
      <div class="cta reveal">
        <a class="btn primary" href="#features">探索功能</a>
        <a class="btn ghost" href="#about">了解更多</a>
      </div>
    </div>
  </section>

  <section class="block" id="features">
    <div class="wrap">
      <h2 class="section-head reveal">核心能力</h2>
      <p class="section-sub reveal">基于你的需求提炼，下面是本站点承诺交付的重点。</p>
      <div class="grid">
{feature_cards}
      </div>
    </div>
  </section>

  <section class="block" id="about">
    <div class="wrap">
      <h2 class="section-head reveal">关于本项目</h2>
      <div class="about glass reveal">
        <p>{about_text}</p>
      </div>
    </div>
  </section>
</main>

<footer class="foot">
  <div class="wrap">
    <span>© {title}</span>
    <span>由 SeedAI 生成 · 可随时对话迭代</span>
  </div>
</footer>

<script>
(function () {{
  var root = document.documentElement;
  var saved = null;
  try {{ saved = localStorage.getItem('seed-theme'); }} catch (e) {{}}
  if (saved) {{ root.setAttribute('data-theme', saved); }}
  var btn = document.getElementById('themeBtn');
  var order = ['system', 'light', 'dark'];
  var label = {{ system: '跟随系统', light: '浅色', dark: '深色' }};
  function current() {{
    var t = root.getAttribute('data-theme');
    return (t === 'light' || t === 'dark') ? t : 'system';
  }}
  function render() {{ btn.textContent = '主题：' + (label[current()]); }}
  render();
  btn.addEventListener('click', function () {{
    var next = order[(order.indexOf(current()) + 1) % order.length];
    if (next === 'system') {{ root.removeAttribute('data-theme'); }} else {{ root.setAttribute('data-theme', next); }}
    try {{ localStorage.setItem('seed-theme', next); }} catch (e) {{}}
    render();
  }});
  var io = new IntersectionObserver(function (entries) {{
    entries.forEach(function (en) {{ if (en.isIntersecting) {{ en.target.classList.add('in'); io.unobserve(en.target); }} }});
  }}, {{ threshold: 0.12 }});
  document.querySelectorAll('.reveal').forEach(function (el) {{ io.observe(el); }});
}})();
</script>
</body>
</html>
"""
        if comp_section:
            html = html.replace("</main>", comp_section + "\n</main>")
        if accent_override:
            html = html.replace("</head>", accent_override + "\n</head>")
        if repair_round:
            # 修复轮：最后一道确定性净化，移除可能残留的危险 token 并兜底结构。
            html = _sanitize_html(html)
        err_hits = await _rag_retrieve(
            settings.chroma_collection_error_patterns,
            f"{title} {' '.join(sections)}",
            top_k=3,
        )
        _check_error_patterns(html, [h.text for h in err_hits])

        logger.info("[produce] 已生成站点 HTML: %d bytes, features=%d, rag_components=%d",
                    len(html.encode("utf-8")), len(features), len(comp_hits))
        return html

    @staticmethod
    def _derive_features(
        sentences: list[str],
        sections: list[str] | None = None,
        styles: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """由 DST 槽位 + 自由文本共同推导 feature 卡片。

        优先展示**结构化板块**（sections 来自 SIR 累积，跨轮稳定），文本句子作为补充，
        这样"第二轮加个联系我"能在产物上真实可见，而不是只改了个标题。
        """
        cards: list[dict[str, str]] = []
        for key in (sections or [])[:6]:
            meta = _SECTION_LABELS.get(key)
            if meta:
                cards.append({"title": meta[0], "desc": meta[1]})
            else:
                cards.append({"title": _truncate(key, 10), "desc": f"{key} 板块。"})
        for s in sentences[: max(0, 6 - len(cards))]:
            words = [w for w in _WORD_SPLIT.split(s) if w]
            title = _truncate(words[0] if words else s, 10) or "要点"
            cards.append({"title": title, "desc": s})
        if styles and len(cards) < 6:
            cards.append({"title": "视觉风格", "desc": "、".join(styles[:5])})
        if not cards:
            cards.append({"title": "需求", "desc": "等待进一步完善站点内容。"})
        return cards

    # ---------------------------------------------------------- Verify
    @staticmethod
    def verify(html: str) -> tuple[bool, str]:
        """结构完整性与注入安全校验（规范 §8.2 Verify），委托给模块级 _verify_html。"""
        return _verify_html(html)

    # ---------------------------------------------------------- Preview
    @staticmethod
    async def preview(
        session: AsyncSession, project: Project, context: TurnContext, html: str
    ) -> tuple[Artifact, str]:
        """原子写入不可变版本目录；委托给模块级 _publish_preview（供 SitePublishTool 复用）。"""
        logger.debug("[preview] project=%s 写入预览产物 (%d bytes)", project.id, len(html.encode("utf-8")))
        return await _publish_preview(session, project, context, html)


async def _publish_preview(
    session: AsyncSession, project: Project, context: TurnContext, html: str
) -> tuple[Artifact, str]:
    """原子写入不可变版本目录，生成 manifest/checksum 与 preview 路径（§8.2 Preview）。

    模块级函数，供 ``SitePublishTool`` 复用，避免逻辑漂移。

    关键步骤(均幂等,版本号单调递增)：
      1. 取当前最大 version 计算 next_version；
      2. 计算 body sha256 + manifest sha256(供后续校验/回滚)；
      3. 先写 ``index.html.tmp`` 再 fsync 后 atomic rename(防半截文件)；
      4. 落 ``Artifact``(不可变版本: status=preview_ready, trace_id=context.trace_id),
         并把 ``project.head_artifact_id`` 指向它、``project.status='active'``、``lock_version+=1``。

    Args:
        session: 数据库会话(本函数内 ``flush``,提交由调用方控制)。
        project: 当前项目。
        context: 本轮 TurnContext(取 user_id / conversation_id / trace_id)。
        html: 已通过 verify 的完整 HTML。
    Returns:
        ``(Artifact, summary_text)``。
    """
    max_version = await session.scalar(
        select(func.max(Artifact.version)).where(Artifact.project_id == project.id)
    )
    version = int(max_version or 0) + 1
    logger.debug("[publish_preview] project=%s next_version=v%d", project.id, version)
    digest_body = hashlib.sha256(html.encode("utf-8")).hexdigest()
    manifest = {
        "index.html": {
            "sha256": digest_body,
            "bytes": len(html.encode("utf-8")),
        }
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    root = Path(settings.artifact_dir) / "previews" / str(context.user.user_id) / str(project.id) / f"v{version}"
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "index.html.tmp"
    target = root / "index.html"
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        file.write(html)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(target)
    logger.debug("[publish_preview] project=%s 写入文件 %s", project.id, target)

    spec_hash = hashlib.sha256(
        json.dumps(project.site_spec, sort_keys=True).encode("utf-8")
    ).hexdigest()
    artifact = Artifact(
        project_id=project.id,
        conversation_id=context.session.conversation_id,
        parent_artifact_id=project.head_artifact_id,
        version=version,
        site_spec_revision=project.lock_version,
        site_spec_hash=spec_hash,
        manifest=manifest,
        manifest_digest=manifest_digest,
        checksums={"index.html": digest_body},
        vendor_manifest_version="seed-premium-v1",
        capability_manifest={"tier": "L0", "theme_toggle": True, "no_external_request": True},
        status="preview_ready",
        preview_path=str(target.relative_to(Path(settings.artifact_dir))),
        trace_id=context.trace_id,
    )
    session.add(artifact)
    await session.flush()
    project.head_artifact_id = artifact.id
    project.status = "active"
    project.lock_version += 1
    logger.info(
        "[publish_preview] project=%s 生成 v%d artifact=%s preview_path=%s",
        project.id, version, artifact.id, artifact.preview_path,
    )
    return artifact, f"已生成网站版本 v{version}，预览产物已就绪。"


site_workflow = SiteWorkflow()
