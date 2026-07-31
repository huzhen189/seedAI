<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'

const props = defineProps<{ content: string }>()
const rendered = ref('')
// rAF 合帧: 外部仍可能高频更新 content 时(如非流式场景), 把多次变更合并到一帧, 避免每变更全量重渲染。
let rafId: number | null = null

function normalizeContent(text: string): string {
  // AI 消息可能存为 JSON 碎片: {"data":"a"}{"data":"b"}... 或单层 {"data":"text"}
  if (text.startsWith('{"data":')) {
    // 多段拼接: 逐段提取 {"data":"x"} → "x"
    const parts: string[] = []
    let pos = 0
    while (true) {
      const start = text.indexOf('{"data":', pos)
      if (start === -1) break
      const end = text.indexOf('}', start)
      if (end === -1) break
      try {
        const seg = JSON.parse(text.slice(start, end + 1))
        if (seg && typeof seg === 'object' && 'data' in seg) {
          parts.push(String(seg.data))
        }
      } catch { /* skip */ }
      pos = end + 1
    }
    if (parts.length) return parts.join('')
    // 单层 JSON
    try {
      const obj = JSON.parse(text)
      if (obj && typeof obj === 'object' && 'data' in obj) {
        return String(obj.data)
      }
    } catch { /* 解析失败, 原样返回 */ }
  }
  return text
}

function render() {
  let src = normalizeContent(props.content)
  // 若内容是大段 HTML 代码(网站产物),包成代码块高亮而非直接渲染成网页
  if (/^\s*<(!DOCTYPE|html)/i.test(src) && src.includes('<')) {
    src = '```html\n' + src + '\n```'
  }
  const raw = marked.parse(src, { breaks: true, gfm: true }) as string
  rendered.value = DOMPurify.sanitize(raw)
  nextTick(() => {
    document.querySelectorAll('.md pre code').forEach((el) => {
      try {
        // 修复 "Element previously highlighted" 警告: 上一轮高亮的 data-highlighted 被 DOMPurify 当
        // data-* 合法属性保留进 rendered, v-html 重注入后元素仍带属性 → 再次 highlight 触发告警。
        // 高亮前先清掉该属性, 让 highlight.js 重新着色。
        ;(el as HTMLElement).removeAttribute('data-highlighted')
        hljs.highlightElement(el as HTMLElement)
      } catch {
        /* ignore */
      }
    })
  })
}

render()

function scheduleRender() {
  if (rafId != null) return
  rafId = requestAnimationFrame(() => {
    rafId = null
    render()
  })
}

watch(() => props.content, scheduleRender)
</script>

<template>
  <!-- 安全:rendered 已先经 DOMPurify.sanitize 清洗(marked 输出不可信),
       此处 v-html 不会引入 XSS。勿移除该清洗步骤。 -->
  <!-- eslint-disable-next-line vue/no-v-html -- 已用 DOMPurify 清洗,安全 -->
  <div class="md" v-html="rendered"></div>
</template>

<style scoped>
.md {
  --md-ink: var(--text-1, #1c2436);
  --md-ink-soft: var(--text-2, #5a667e);
  --md-ink-faint: var(--text-3, #8b95aa);
  --md-border: var(--border, rgba(20, 30, 60, 0.1));
  --md-brand: var(--brand, #5b8cff);
  --md-surface: var(--surface, rgba(255, 255, 255, 0.7));
  --md-bg: var(--bg, #f4f6fb);
  color: var(--md-ink);
  line-height: 1.75;
  font-size: 14.5px;
  word-break: break-word;
}
/* 标题层级 + 锚点 */
.md :deep(h1),
.md :deep(h2),
.md :deep(h3),
.md :deep(h4) {
  color: var(--md-ink);
  line-height: 1.25;
  letter-spacing: -0.01em;
  margin: 1.6em 0 0.7em;
  font-weight: 700;
  scroll-margin-top: 80px;
}
.md :deep(h1) { font-size: 1.9rem; padding-bottom: 0.3em; border-bottom: 1px solid var(--md-border); }
.md :deep(h2) { font-size: 1.5rem; padding-bottom: 0.25em; border-bottom: 1px solid var(--md-border); }
.md :deep(h3) { font-size: 1.22rem; }
.md :deep(h4) { font-size: 1.05rem; }
/* 标题悬停锚点提示 */
.md :deep(h1):hover::before,
.md :deep(h2):hover::before,
.md :deep(h3):hover::before {
  content: "#";
  color: var(--md-brand);
  font-weight: 700;
  margin-right: 6px;
  opacity: 0.6;
}

.md :deep(p) { margin: 0 0 1em; color: var(--md-ink); }
.md :deep(ul),
.md :deep(ol) { padding-left: 1.4em; margin: 0 0 1em; }
.md :deep(li) { margin: 0.3em 0; }
.md :deep(li)::marker { color: var(--md-brand); }

.md :deep(a) { color: var(--md-brand); text-decoration: none; border-bottom: 1px solid transparent; transition: border-color 0.2s; }
.md :deep(a):hover { border-bottom-color: var(--md-brand); }

/* 行内代码 vs 代码块 */
.md :deep(:not(pre) > code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.88em;
  padding: 0.15em 0.45em;
  border-radius: 6px;
  color: #b8326a;
  background: color-mix(in srgb, var(--md-brand) 10%, transparent);
  border: 1px solid var(--md-border);
  word-break: break-word;
}
.md :deep(pre) {
  position: relative;
  background: #0f172a;
  color: #e6edf3;
  padding: 16px 18px;
  border-radius: 12px;
  overflow: auto;
  margin: 0 0 1.2em;
  box-shadow: 0 8px 22px -12px rgba(0, 0, 0, 0.4);
}
.md :deep(pre code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.6;
  background: none;
  border: 0;
  padding: 0;
  color: inherit;
}

/* 引用块 / callout */
.md :deep(blockquote) {
  margin: 1em 0;
  padding: 12px 18px;
  border-left: 4px solid var(--md-brand);
  border-radius: 0 10px 10px 0;
  background: color-mix(in srgb, var(--md-brand) 8%, transparent);
  color: var(--md-ink-soft);
}
.md :deep(blockquote) p { margin: 0.2em 0; color: inherit; }

/* 表格美化 */
.md :deep(table) {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  margin: 0 0 1.2em;
  border: 1px solid var(--md-border);
  border-radius: 12px;
  overflow: hidden;
  font-size: 14px;
}
.md :deep(th),
.md :deep(td) {
  padding: 10px 14px;
  border-bottom: 1px solid var(--md-border);
  text-align: left;
}
.md :deep(th) {
  background: color-mix(in srgb, var(--md-brand) 12%, transparent);
  color: var(--md-ink);
  font-weight: 700;
}
.md :deep(tbody tr:nth-child(even)) { background: color-mix(in srgb, var(--md-ink) 3%, transparent); }
.md :deep(tbody tr:last-child td) { border-bottom: 0; }
.md :deep(td) code { background: color-mix(in srgb, var(--md-brand) 10%, transparent); }

/* 分隔线 */
.md :deep(hr) {
  border: 0;
  height: 1px;
  background: var(--md-border);
  margin: 1.8em 0;
}

/* 图片: 圆角 + 响应式 + 间距 */
.md :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 12px;
  margin: 0.5em 0;
  box-shadow: 0 8px 22px -14px rgba(0, 0, 0, 0.35);
  display: block;
}

/* 强调 */
.md :deep(strong) { color: var(--md-ink); font-weight: 700; }
.md :deep(mark) {
  background: color-mix(in srgb, var(--md-brand) 25%, transparent);
  color: inherit;
  padding: 0.05em 0.3em;
  border-radius: 4px;
}
</style>
