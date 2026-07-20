<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import type { Artifact } from '../types'

const props = defineProps<{
  artifacts: Artifact[]
  generating: boolean
  generatedHtml: string
  previewUrl: string | null
  projectId: number | null
}>()

const emit = defineEmits<{ refresh: [] }>()

// ---- COS 重传轮询 ----
const uploading = computed(() => props.artifacts.some(a => a.status === 'uploading'))
let retryTimer: ReturnType<typeof setInterval> | null = null

async function checkPendingUploads() {
  if (!props.projectId) return
  if (!uploading.value) { stopRetry(); return }
  try {
    const resp = await fetch(`/api/projects/${props.projectId}/retry-upload`, { method: 'POST' })
    const data = await resp.json()
    if (data.results?.some((r: any) => r.ok)) {
      stopRetry()
      emit('refresh')
    }
  } catch { /* 网络错误, 等下一轮 */ }
}

function startRetry() {
  if (retryTimer) return
  retryTimer = setInterval(checkPendingUploads, 600000) // 10 分钟
}

function stopRetry() {
  if (retryTimer) { clearInterval(retryTimer); retryTimer = null }
}

onMounted(() => { if (uploading.value) startRetry() })
onUnmounted(() => stopRetry())

const collapsed = ref(false)
const selectedFile = ref<string | null>(null)

function ext(name: string) {
  const i = name.lastIndexOf('.')
  return i > 0 ? name.slice(i + 1).toLowerCase() : ''
}

function sizeKB(n: number) {
  return (n / 1024).toFixed(1) + 'KB'
}

function iconFor(name: string) {
  const e = ext(name)
  const map: Record<string, string> = {
    html: '🌐', css: '🎨', js: '⚡', json: '📋', svg: '🖼️',
    png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', webp: '🖼️',
    md: '📝', txt: '📄', py: '🐍', ts: '🔷', zip: '📦',
  }
  return map[e] || '📄'
}

function previewMode(name: string) {
  const e = ext(name)
  if (['html', 'htm'].includes(e)) return 'html'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(e)) return 'image'
  return 'code'
}

function selectFile(name: string) {
  selectedFile.value = name
}

// 所有产物文件展平为统一列表
const allFiles = computed(() => {
  const list: { name: string; size: number; url: string; content?: string; artifact: Artifact }[] = []
  for (const a of props.artifacts) {
    if (!a.files) continue
    for (const [name, info] of Object.entries(a.files)) {
      list.push({
        name,
        size: (info as any).size || 0,
        url: (info as any).url || '',
        content: (info as any).content || '',
        artifact: a,
      })
    }
  }
  return list
})

const currentFile = computed(() =>
  selectedFile.value
    ? allFiles.value.find(f => f.name === selectedFile.value)
    : allFiles.value.find(f => ext(f.name) === 'html') || allFiles.value[0]
)

const mode = computed(() => currentFile.value ? previewMode(currentFile.value.name) : 'none')
</script>

<template>
  <!-- 收起/展开切换栏 -->
  <div class="right-toggle">
    <button class="toggle-btn" @click="collapsed = !collapsed">
      {{ collapsed ? '◀' : '▶' }}
    </button>
    <span v-if="collapsed" class="toggle-label">{{ allFiles.length }} 文件</span>
  </div>

  <div v-if="!collapsed" class="right-body">
    <!-- 左侧文件树 -->
    <div class="file-tree">
      <div class="tree-head">📁 文件</div>
      <div v-if="uploading" class="upload-banner">
        <span class="spinner-sm"></span> COS 上传中…
        <button class="retry-btn" @click="startRetry(); checkPendingUploads()">重试</button>
      </div>
      <div v-if="generating && !allFiles.length" class="tree-empty">AI 正在生成…</div>
      <template v-for="f in allFiles" :key="f.name">
        <div
          class="tree-item"
          :class="{ active: selectedFile === f.name || (!selectedFile && f === allFiles[0]) }"
          @click="selectFile(f.name)"
        >
          <span class="tree-icon">{{ iconFor(f.name) }}</span>
          <span class="tree-name">{{ f.name }}</span>
          <span class="tree-size">{{ sizeKB(f.size) }}</span>
        </div>
      </template>
      <div v-if="!generating && !allFiles.length" class="tree-empty">暂无文件</div>
    </div>

    <!-- 右侧预览区 -->
    <div class="preview-area">
      <!-- HTML 预览: COS 直链 > srcdoc 本地渲染 -->
      <iframe
        v-if="mode === 'html' && currentFile"
        class="pv-frame"
        :src="currentFile.url || undefined"
        :srcdoc="currentFile.url ? undefined : (currentFile.content || generatedHtml)"
        sandbox="allow-scripts allow-same-origin allow-forms"
        title="preview"
      ></iframe>

      <!-- 图片预览 -->
      <div v-else-if="mode === 'image' && currentFile?.url" class="pv-image">
        <img :src="currentFile.url" :alt="currentFile.name" />
      </div>

      <!-- 代码/文本预览 — 从 artifact 的 file content 展示 -->
      <div v-else-if="mode === 'code'" class="pv-code">
        <div class="pv-code-head">{{ currentFile?.name }}</div>
        <pre><code>{{ (currentFile?.artifact.files?.[currentFile?.name || ''] as any)?.content || '(二进制文件，无法预览)' }}</code></pre>
      </div>

      <!-- 生成中占位 -->
      <div v-if="generating" class="pv-placeholder">
        <div class="spinner"></div>
        <span>AI 正在生成…</span>
      </div>

      <!-- 空状态 -->
      <div v-if="!generating && !allFiles.length" class="pv-placeholder">
        <span>暂无生成产物</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.right-toggle {
  display: flex;
  align-items: center;
  padding: 6px 8px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.toggle-btn {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
  padding: 2px 8px;
  font-size: 12px;
  color: var(--muted);
}
.toggle-label { font-size: 12px; color: var(--muted); margin-left: 6px; }

.right-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

/* ---- 文件树 ---- */
.file-tree {
  width: 30%;
  min-width: 140px;
  max-width: 220px;
  border-right: 1px solid var(--border);
  background: var(--panel);
  overflow-y: auto;
  padding: 6px 0;
}
.tree-head {
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  padding: 4px 10px;
}
.tree-empty {
  padding: 10px;
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
}
.upload-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 11px;
  color: #b45309;
  background: #fffbeb;
  border-bottom: 1px solid #fde68a;
}
.spinner-sm {
  width: 12px; height: 12px;
  border: 2px solid #fde68a;
  border-top-color: #f59e0b;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
.retry-btn {
  margin-left: auto;
  border: 1px solid #f59e0b;
  background: transparent;
  color: #b45309;
  border-radius: 4px;
  cursor: pointer;
  font-size: 10px;
  padding: 1px 6px;
}
.tree-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  cursor: pointer;
  font-size: 12px;
  border-left: 2px solid transparent;
  transition: background 0.15s;
}
.tree-item:hover { background: #f1f5f9; }
.tree-item.active {
  background: #eef2ff;
  border-left-color: var(--brand);
  font-weight: 600;
}
.tree-icon { font-size: 14px; flex-shrink: 0; }
.tree-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-size { font-size: 10px; color: var(--muted); flex-shrink: 0; }

/* ---- 预览区 ---- */
.preview-area {
  flex: 1;
  min-width: 0;
  position: relative;
  background: #fff;
}
.pv-frame {
  width: 100%;
  height: 100%;
  border: 0;
}
.pv-image {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 20px;
}
.pv-image img { max-width: 100%; max-height: 100%; object-fit: contain; }
.pv-code {
  height: 100%;
  overflow: auto;
  padding: 12px;
}
.pv-code-head {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 8px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
.pv-code pre {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: #334155;
  white-space: pre-wrap;
  word-break: break-all;
}
.pv-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--muted);
  gap: 10px;
}
.spinner {
  width: 24px;
  height: 24px;
  border: 3px solid var(--border);
  border-top-color: var(--brand);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
