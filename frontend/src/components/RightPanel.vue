<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import type { Artifact } from '../types'

const props = defineProps<{
  artifacts: Artifact[]          // 生成的文件(COS/本地)
  generating: boolean
  previewUrl: string | null       // COS直链
  projectId: number | null
  requirementDoc: Record<string, any> | null  // 需求文档
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

function previewMode(name: string): 'html' | 'image' | 'code' | 'requirement' {
  if (name === '__requirement_doc__') return 'requirement'
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

const mode = computed(() => {
  if (selectedFile.value === '__requirement_doc__') return 'requirement'
  return currentFile.value ? previewMode(currentFile.value.name) : 'none'
})

// 暴露给父组件(ChatView): 点击文字产物链接时, 联动右侧预览选中对应文件并打开。
defineExpose({ selectFile })
</script>

<template>
  <div class="rp-body">
  <!-- 需求文档(伪目录) -->
  <div v-if="requirementDoc" class="req-tree">
      <div class="tree-head">📋 需求文档</div>
      <div
        class="tree-item"
        :class="{ active: selectedFile === '__requirement_doc__' }"
        @click="selectFile('__requirement_doc__')"
      >
        <span class="tree-icon">📄</span>
        <span class="tree-name">{{ requirementDoc.brand?.name || '需求文档' }}.txt</span>
        <span class="tree-size">需求</span>
      </div>
    </div>

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

    <!-- 右侧预览区: 仅展示生成的文件内容 -->
    <div class="preview-area">
      <!-- 需求文档预览(优先) -->
      <div v-if="mode === 'requirement'" class="pv-requirement">
        <div class="pv-code-head">📋 {{ requirementDoc?.brand?.name || '需求文档' }}</div>
        <div class="req-body">
          <div v-if="requirementDoc?.brand" class="req-section">
            <h4>🏷 品牌</h4>
            <p><strong>{{ requirementDoc.brand.name }}</strong> — {{ requirementDoc.brand.slogan }}</p>
            <p class="req-intro">{{ requirementDoc.brand.intro }}</p>
          </div>
          <div v-if="requirementDoc?.target_user" class="req-section">
            <h4>👥 目标用户</h4>
            <p>{{ requirementDoc.target_user }}</p>
          </div>
          <div v-if="requirementDoc?.pages?.length" class="req-section">
            <h4>📑 页面结构</h4>
            <div v-for="p in requirementDoc.pages" :key="p.title" class="req-page">
              <p><strong>{{ p.title }}</strong></p>
              <ul v-if="p.sections?.length">
                <li v-for="s in p.sections" :key="s.name">{{ s.name }}: {{ s.content?.substring(0, 60) }}</li>
              </ul>
            </div>
          </div>
          <div v-if="requirementDoc?.features?.length" class="req-section">
            <h4>⚙ 功能清单</h4>
            <div class="req-tags">
              <span v-for="f in requirementDoc.features" :key="f" class="req-tag">{{ f }}</span>
            </div>
          </div>
          <div v-if="requirementDoc?.design_style" class="req-section">
            <h4>🎨 设计风格</h4>
            <p>{{ requirementDoc.design_style }}
              <span v-if="requirementDoc.color_scheme"
                class="req-color" :style="{background: requirementDoc.color_scheme.primary}"></span>
            </p>
          </div>
        </div>
      </div>

      <!-- HTML 预览(生成的文件) -->
      <iframe
        v-else-if="mode === 'html' && currentFile"
        class="pv-frame"
        :src="currentFile.url || undefined"
        :srcdoc="currentFile.url ? undefined : ((currentFile.artifact?.files?.[currentFile.name || ''] as any)?.content || '')"
        sandbox="allow-scripts allow-forms"
        title="preview"
      ></iframe>

      <!-- 图片预览 -->
      <div v-else-if="mode === 'image' && currentFile?.url" class="pv-image">
        <img :src="currentFile.url" :alt="currentFile.name" />
      </div>

      <!-- 代码/文本预览 -->
      <div v-else-if="mode === 'code'" class="pv-code">
        <div class="pv-code-head">{{ currentFile?.name }}</div>
        <pre><code>{{ (currentFile?.artifact.files?.[currentFile?.name || ''] as any)?.content || '(二进制文件，无法预览)' }}</code></pre>
      </div>

      <!-- 生成中 -->
      <div v-else-if="generating && !allFiles.length" class="pv-placeholder">
        <div class="spinner"></div>
        <span>AI 正在生成…</span>
      </div>

      <!-- 空状态 -->
      <div v-else class="pv-placeholder">
        <span>暂无生成产物</span>
        <span class="pv-hint">文件生成后将在此预览</span>
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

.rp-body {
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
  gap: 6px;
}
.pv-hint { font-size: 11px; opacity: 0.6; }
.spinner {
  width: 24px;
  height: 24px;
  border: 3px solid var(--border);
  border-top-color: var(--brand);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ---- 需求文档 ---- */
.req-tree {
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
.req-tree .tree-head {
  color: var(--accent, #6366f1);
}
.pv-requirement {
  padding: 16px;
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.6;
}
.pv-requirement h4 {
  margin: 12px 0 4px;
  font-size: 14px;
  color: var(--text);
}
.req-intro {
  color: var(--text-muted, #888);
  font-size: 12px;
}
.req-page p { margin: 2px 0; }
.req-page ul { margin: 0 0 8px 16px; }
.req-page li { font-size: 12px; color: var(--text-muted, #666); }
.req-tags {
  display: flex; gap: 6px; flex-wrap: wrap;
}
.req-tag {
  background: var(--accent-light, #eef2ff);
  color: var(--accent, #6366f1);
  padding: 2px 8px; border-radius: 10px; font-size: 12px;
}
.req-color {
  display: inline-block; width: 14px; height: 14px;
  border-radius: 3px; vertical-align: middle; margin-left: 6px;
  border: 1px solid var(--border);
}
</style>
