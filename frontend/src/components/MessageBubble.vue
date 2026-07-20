<script setup lang="ts">
import { ref, computed } from 'vue'
import MarkdownView from './MarkdownView.vue'
import type { ContentData } from '../types'

const props = defineProps<{ role: string; content: string; time?: string }>()
const expanded = ref(false)

function parseContent(c: string): ContentData {
  if (c.startsWith('{') && c.includes('"type"')) {
    try {
      const obj = JSON.parse(c)
      if (obj && obj.type) return obj as ContentData
    } catch { /* ignore */ }
  }
  return { type: 'plain', text: c }
}

const data = computed(() => parseContent(props.content))

const isExpandable = computed(() =>
  data.value.type === 'plain' && data.value.text.length > 2000
)

function fmtTime(t: string): string {
  if (!t) return ''
  const d = new Date(t)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="bubble" :class="role">
    <div class="role">{{ role === 'user' ? '你' : 'AI' }}<span v-if="time" class="time">{{ fmtTime(time) }}</span></div>
    <div class="body" :class="{ expanded: expanded }">
      <!-- 纯文本 / 闲聊 -->
      <MarkdownView v-if="data.type === 'plain' && role === 'assistant'" :content="data.text" />
      <span v-else-if="data.type === 'plain'">{{ data.text }}</span>
      <!-- 建站产物 -->
      <div v-else-if="data.type === 'site'" class="site-card">
        <div class="site-title">🌐 {{ data.title }}</div>
        <div class="site-links">
          <a :href="data.preview_url" target="_blank" class="btn">🔗 预览</a>
          <a v-if="data.download_url" :href="data.download_url" class="btn">📥 下载</a>
        </div>
        <div v-if="data.files?.length" class="site-files">
          <span v-for="f in data.files" :key="f.name" class="file-tag">{{ f.name }} ({{ (f.size / 1024).toFixed(1) }}KB)</span>
        </div>
      </div>
      <!-- 代码产物 -->
      <div v-else-if="data.type === 'code'" class="code-card">
        <div class="code-title">📄 {{ data.title }}</div>
        <pre v-if="data.code_preview" class="code-preview"><code>{{ data.code_preview }}</code></pre>
      </div>
      <!-- 错误消息 -->
      <div v-else-if="data.type === 'error'" class="error-card">⚠️ {{ data.message }}</div>
      <!-- 兜底 -->
      <span v-else>{{ content }}</span>
    </div>
    <button v-if="isExpandable && !expanded" class="expand" @click="expanded = true">展开全部 ▾</button>
    <button v-if="isExpandable && expanded" class="expand" @click="expanded = false">收起 ▲</button>
  </div>
</template>

<style scoped>
.bubble {
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 14px;
  line-height: 1.6;
}
.bubble.user {
  background: #eef2ff;
  border: 1px solid #e0e7ff;
  align-self: flex-end;
  max-width: 90%;
}
.bubble.assistant {
  background: #fff;
  border: 1px solid var(--border);
  max-width: 100%;
}
.body { max-height: 50vh; overflow-y: auto; }
.body.expanded { max-height: none; overflow-y: visible; }
.expand {
  margin-top: 6px; font-size: 12px; color: var(--brand); cursor: pointer;
  border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; background: #fff;
}
.time { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }

/* ---- Site Card ---- */
.site-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 12px;
}
.site-title { font-weight: 600; font-size: 15px; margin-bottom: 8px; }
.site-links { display: flex; gap: 8px; margin-bottom: 8px; }
.site-links .btn {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 12px; font-size: 13px; border-radius: 6px;
  background: var(--brand, #4f46e5); color: #fff; text-decoration: none;
}
.site-links .btn:hover { opacity: 0.9; }
.site-files { display: flex; flex-wrap: wrap; gap: 4px; }
.file-tag {
  font-size: 11px; background: #e2e8f0; padding: 2px 6px; border-radius: 4px;
  color: #475569;
}

/* ---- Code Card ---- */
.code-card { background: #1e1e1e; border-radius: 8px; padding: 10px; }
.code-title { color: #ccc; font-size: 13px; margin-bottom: 6px; }
.code-preview { max-height: 300px; overflow: auto; margin: 0; }
.code-preview code { color: #d4d4d4; font-size: 12px; }

/* ---- Error Card ---- */
.error-card {
  background: #fef2f2; border: 1px solid #fecaca;
  border-radius: 8px; padding: 8px 12px; color: #b91c1c; font-size: 13px;
}
</style>
