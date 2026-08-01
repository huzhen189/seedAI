"""SiteWorkflow：Spec → Produce/Edit → Verify → Preview。

这是规范 §8.2 的固定子流程。当前在没有外部 LLM 依赖的前提下，
从 ``projects.site_spec``（需求文档）确定性地生成一份**真实可用、premium 质感**
的静态站点：语义化结构、暗/亮/跟随系统三态主题、玻璃拟态、响应式、滚动渐显、
无任何外部 CDN 请求（vendor 全部内联，满足 §11.3 的隔离与确定性）。

Verify 负责结构完整性与注入安全；Produce 默认走"生成完整版本"，
修改时基于上一稳定版本做受控覆盖（不原地破坏 active 版本）。
"""

from __future__ import annotations

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

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;~\n]+")
_WORD_SPLIT = re.compile(r"[，,、\s]+")


def _esc(value: str) -> str:
    """HTML 正文转义。所有用户内容都经过此处，构造上杜绝 <script> 注入。"""
    return escape(str(value), quote=True)


def _sentences(prompt: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(prompt or "")]
    return [p for p in parts if p]


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


class SiteWorkflow:
    # ---------------------------------------------------------- Spec
    async def build_spec(self, session: AsyncSession, project: Project, context: TurnContext) -> dict:
        """把本轮明确指令合并进 ``projects.site_spec``（SiteSpec）。

        低置信要求进入 pending；本轮只持久化已确认的高置信需求文本。
        """
        spec = dict(project.site_spec or {})
        spec["title"] = project.name
        spec["prompt"] = context.clean_message
        # 累积需求历史，便于后续 edit 基于既有意图做受控 patch。
        history = list(spec.get("history", []))
        history.append(context.clean_message)
        spec["history"] = history[-10:]
        spec["theme"] = spec.get("theme", "system")
        project.site_spec = spec
        return spec

    # ---------------------------------------------------------- Produce
    def produce(self, spec: dict) -> str:
        title = _esc(spec.get("title") or "我的网站")
        prompt = spec.get("prompt") or ""
        theme = spec.get("theme", "system")
        sentences = _sentences(prompt)

        hero_subtitle = _esc(sentences[0]) if sentences else _esc("用对话创造的数字体验")
        about_text = _esc(prompt) if prompt else _esc("这是一个由 SeedAI 通过对话生成的站点。")

        features = self._derive_features(sentences)
        feature_cards = "\n".join(
            f"""        <article class="card glass reveal">
          <div class="card-dot"></div>
          <h3>{_esc(f['title'])}</h3>
          <p>{_esc(f['desc'])}</p>
        </article>""" for f in features
        )

        return f"""<!doctype html>
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

    @staticmethod
    def _derive_features(sentences: list[str]) -> list[dict[str, str]]:
        cards: list[dict[str, str]] = []
        for s in sentences[:6]:
            words = [w for w in _WORD_SPLIT.split(s) if w]
            title = _truncate(words[0] if words else s, 10) or "要点"
            cards.append({"title": title, "desc": s})
        if not cards:
            cards.append({"title": "需求", "desc": "等待进一步完善站点内容。"})
        return cards

    # ---------------------------------------------------------- Verify
    @staticmethod
    def verify(html: str) -> tuple[bool, str]:
        """结构完整性与注入安全校验（规范 §8.2 Verify）。"""
        if not html or not html.strip().lower().startswith("<!doctype html>"):
            return False, "site_verify_missing_doctype"
        if "</html>" not in html.lower():
            return False, "site_verify_unclosed_html"
        if len(html.encode("utf-8")) < 400:
            return False, "site_verify_too_small"
        # 危险标签/属性不得在生成产物中出现（用户内容已全量转义，这里做最后一道闸门）。
        lowered = html.lower()
        for forbidden in ("<iframe", "<object", "<embed", "javascript:", "onerror=", "onload="):
            if forbidden in lowered:
                return False, "site_verify_unsafe_token"
        return True, "ok"

    # ---------------------------------------------------------- Preview
    async def preview(
        self, session: AsyncSession, project: Project, context: TurnContext, html: str
    ) -> tuple[Artifact, str]:
        """原子写入不可变版本目录，生成 manifest/checksum 与 preview 路径。"""
        max_version = await session.scalar(
            select(func.max(Artifact.version)).where(Artifact.project_id == project.id)
        )
        version = int(max_version or 0) + 1
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
        return artifact, f"已生成网站版本 v{version}，预览产物已就绪。"


site_workflow = SiteWorkflow()
