<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'

const props = defineProps<{ content: string }>()
const rendered = ref('')

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
        hljs.highlightElement(el as HTMLElement)
      } catch {
        /* ignore */
      }
    })
  })
}

render()
watch(() => props.content, render)
</script>

<template>
  <!-- 安全:rendered 已先经 DOMPurify.sanitize 清洗(marked 输出不可信),
       此处 v-html 不会引入 XSS。勿移除该清洗步骤。 -->
  <!-- eslint-disable-next-line vue/no-v-html -- 已用 DOMPurify 清洗,安全 -->
  <div class="md" v-html="rendered"></div>
</template>

<style scoped>
.md {
  line-height: 1.7;
  font-size: 14px;
  word-break: break-word;
}
.md :deep(pre) {
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px 14px;
  border-radius: 10px;
  overflow: auto;
}
.md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
}
.md :deep(p) {
  margin: 0 0 10px;
}
.md :deep(h1),
.md :deep(h2),
.md :deep(h3) {
  margin: 14px 0 8px;
}
.md :deep(ul),
.md :deep(ol) {
  padding-left: 20px;
  margin: 0 0 10px;
}
.md :deep(a) {
  color: var(--brand);
}
.md :deep(table) {
  border-collapse: collapse;
}
.md :deep(th),
.md :deep(td) {
  border: 1px solid var(--border);
  padding: 4px 8px;
}
</style>
