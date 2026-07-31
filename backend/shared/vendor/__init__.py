"""SeedPremium 本地设计系统 — 加载器与注入工具。

本模块是「网站生成」唯一内置 CSS 组件库的入口:
  - SEED_PREMIUM_CSS / SEED_PREMIUM_JS: 设计系统源码(可读文本, 用于落盘内联);
  - VENDOR_REFERENCE: 供 LLM prompt 注入的 class 速查表(让模型只引用白名单 class);
  - ensure_vendor(html): 落盘阶段保证 HTML 内含本设计系统(无外部 CDN 时内联)。

设计原则: 小而美、零依赖、可断网运行 —— 根治模型乱引外部 CDN / 占位图导致预览变灰块。
"""
from __future__ import annotations

import json
from pathlib import Path

_VENDOR_DIR = Path(__file__).resolve().parent

_SEED_CSS_PATH = _VENDOR_DIR / "seed-premium.css"
_SEED_JS_PATH = _VENDOR_DIR / "seed-premium.js"

_LIBS_DIR = _VENDOR_DIR / "libs"
_LIBS_INDEX_PATH = _LIBS_DIR / "libs_index.json"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


SEED_PREMIUM_CSS: str = _read(_SEED_CSS_PATH)
SEED_PREMIUM_JS: str = _read(_SEED_JS_PATH)


def _load_libs_index() -> list:
    """读取本地组件库清单(由 _download_libs.py 生成)。返回 list[dict]。"""
    try:
        raw = json.loads(_LIBS_INDEX_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw
    except Exception:
        pass
    return []


# 本地预置组件库清单(根绝对路径 /vendor/libs/...)。发布到服务器后该目录位于域名根,全站共享。
LIBS_INDEX: list = _load_libs_index()


def _build_libs_reference() -> str:
    """从 LIBS_INDEX 自动生成 LLM 白名单: 每个库给出精确 /vendor/libs/... 引用路径。"""
    if not LIBS_INDEX:
        return ""
    lines = [
        "【本地组件库白名单 · 可选增强, 全部已预置在服务器 /vendor/libs/ 下】",
        "需要时按下方给出的【根绝对路径】以 <script src=\"/vendor/libs/<name>/<file>\"> 或 "
        "<link href=\"/vendor/libs/<name>/<file>\"> 引入; 发布到你的服务器后该目录位于「域名根 /vendor/libs/」,"
        "全站共享同一份, 无需重复下载, 也不会有跨域/离线白屏问题。",
    ]
    for e in LIBS_INDEX:
        refs = [j["path"] for j in e.get("js", [])] + [c["path"] for c in e.get("css", [])]
        g = e.get("global") or "纯CSS/按需"
        lines.append(
            f"• {e['name']} (v{e['version']}, 全局变量 `{g}`): {e.get('desc', '')} \n    引用: "
            + "  ".join(refs)
        )
    lines.append(
        "⚠️ 只可从上述白名单选择, 引用路径一律用 /vendor/libs/... 根绝对形式;"
        "禁止再引入任何 unpkg / jsdelivr / cdn.tailwindcss / cdnjs / code.jquery 等外部 CDN(离线/沙箱预览会变灰块/白屏)。"
    )
    return "\n".join(lines)


# 供 LLM prompt 注入的组件库速查(与 VENDOR_REFERENCE 一起拼进 SYS_CODER)
LIBS_REFERENCE: str = _build_libs_reference()


# —— 供 LLM 引用的 class 白名单速查(注入 SYS_CODER, 让模型只依赖本系统)——
VENDOR_REFERENCE: str = (
    "【设计系统白名单 · 仅允许使用以下 class,禁止引入任何外部 CDN / 框架】\n"
    "本平台已内置 SeedPremium 玻璃拟态设计系统,你必须只使用下列 class(已随页面内联,勿外引资源):\n"
    "• 布局: .container .section .section-title .section-sub .grid .grid-2 .grid-3 .grid-4\n"
    "• 玻璃/卡片: .glass .card  (卡片加 .reveal 可滚动渐显)\n"
    "• 按钮: .btn .btn-primary .btn-ghost  磁吸效果加属性 data-magnetic=\"0.25\"\n"
    "• 标签: .tag .tag-accent\n"
    "• Hero: .hero .hero .display .lead .hero-cta\n"
    "• 导航: .nav .nav-brand .nav-links .nav-toggle (移动端 .nav-links.open 展开)\n"
    "• 表格: .table (配合 <thead><tbody>,th/td 自动美化)\n"
    "• 引用/代码: .quote  block: pre.code  行内: 用 `code` 自动样式\n"
    "• 表单: .field .input .textarea\n"
    "• 其它: .badge-dot(纯CSS图标圆) .hr(分隔线) .display(巨幅标题)\n"
    "• 主题: 深色请在 <html class=\"dark\"> 上切换; 配色改 :root 的 --brand/--brand-2/--bg 等变量即可。\n"
    "• 排版: 直接用 h1/h2/h3/.display/.lead/p/ul/ol/a,已含编辑级层级与行高。\n"
    "• 进场动画: 给任意元素加 class=\"reveal\",进入视口自动渐显位移(JS 已内联)。\n"
    "• 图片: 一律使用真实图片 URL 或 SVG;严禁 via.placeholder.com 等占位图(预览会变灰块)。\n"
    "⚠️ 严禁 <script src=...unpkg/jsdelivr/cdn.tailwindcss> 或 React/Babel CDN; "
    "本系统已通过内联 <style>/<script> 提供全部样式与交互。"
)


def ensure_vendor(
    html: str,
    *,
    css: str | None = None,
    js: str | None = None,
    force: bool = False,
) -> str:
    """落盘阶段保证 HTML 内含 SeedPremium(无外部依赖时内联)。

    - 若 HTML 已含 `/artifacts/_vendor/` 引用或已内联我们的标记,且非 force,则原样返回;
    - 否则把设计系统 CSS 注入 <head>(在 </head> 前或 <html> 后),JS 注入 </body> 前;
    - force=True 时无论是否已引用,都补一次内联(幂等: 已含标记会跳过对应块)。
    返回处理后的 HTML 字符串。
    """
    css = css if css is not None else SEED_PREMIUM_CSS
    js = js if js is not None else SEED_PREMIUM_JS
    out = html

    _CSS_MARK = "/* SeedPremium */"
    _JS_MARK = "/* SeedPremium JS */"
    if css and _CSS_MARK not in out:
        block = f"<style>\n{_CSS_MARK}\n{css}\n</style>"
        if "</head>" in out:
            out = out.replace("</head>", block + "\n</head>", 1)
        elif "<head>" in out:
            out = out.replace("<head>", "<head>\n" + block, 1)
        elif "<html" in out:
            out = out.replace("</html>", block + "\n</html>", 1) if "</html>" in out else out + block
        else:
            out = block + "\n" + out

    if js and _JS_MARK not in out:
        block = f"<script>\n{_JS_MARK}\n{js}\n</script>"
        if "</body>" in out:
            out = out.replace("</body>", block + "\n</body>", 1)
        else:
            out = out + block

    return out
