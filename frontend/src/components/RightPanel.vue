<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import type { Artifact } from '../types'
import MarkdownView from './MarkdownView.vue'

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


const selectedArtifactId = ref<number | null>(null)
const selectedName = ref<string>('')

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

function previewMode(name: string): 'html' | 'image' | 'code' | 'md' | 'requirement' {
  if (name === '__requirement_doc__') return 'requirement'
  const e = ext(name)
  if (['html', 'htm'].includes(e)) return 'html'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(e)) return 'image'
  if (['md', 'markdown'].includes(e)) return 'md'
  return 'code'
}

// 兼容 ChatView 旧调用 selectFile(name) 与文件树精确调用 selectFile(artifactId, name)
function selectFile(a: number | string | null, b?: string) {
  if (typeof a === 'number') {
    selectedArtifactId.value = a
    selectedName.value = b || ''
  } else {
    selectedArtifactId.value = null
    selectedName.value = (a as string) || ''
  }
  // 根据文件类型重置默认视图: HTML/MD 默认预览, CSS/JS 默认源码
  const target = b || (a as string) || ''
  const e = ext(target)
  if (['html', 'htm', 'md'].includes(e)) {
    currentView.value = 'preview'
  } else if (['css', 'js', 'json', 'txt', 'py', 'ts', 'svg'].includes(e)) {
    currentView.value = 'code'
  }
}

// 当前视图模式: 'preview'(预览) | 'code'(源码)
const currentView = ref<'preview' | 'code'>('preview')

// 新产物到来时自动选中最新 HTML 文件并切到预览。
// 触发条件(a)数量增加 (b)最新条 trace_id 变化(新一轮生成) → 强制切回预览, 不卡在旧代码视图。
watch(
  () => props.artifacts,
  (now, prev) => {
    if (!now || now.length === 0) return
    const latestNow = now[now.length - 1]
    const latestPrev = prev && prev.length ? prev[prev.length - 1] : null
    const sameAsBefore = latestPrev &&
      prev!.length === now.length &&
      latestPrev.id === latestNow.id &&
      (latestPrev.trace_id || null) === (latestNow.trace_id || null)
    if (sameAsBefore) return  // 同一产物重加载(如 loadArtifacts 重试), 不抖动
    if (!latestNow?.files) return
    const names = Array.isArray(latestNow.files) ? (latestNow.files as any[]).map((f: any) => f.name) : Object.keys(latestNow.files)
    const htmlName = names.find((n: string) => ['html', 'htm'].includes((n.split('.').pop() || '').toLowerCase()))
    if (htmlName) {
      selectFile(latestNow.id, htmlName)
      currentView.value = 'preview'
    }
  },
)

// 当前文件类型判断(模板用)
const currentFileIsHTML = computed(() => currentFile.value && ['html', 'htm'].includes(ext(currentFile.value.name)))
const currentFileIsMD   = computed(() => currentFile.value && ['md'].includes(ext(currentFile.value.name)))
const currentFileIsImage = computed(() => currentFile.value && ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext(currentFile.value.name)))

// 切换视图: preview 模式下 HTML/MD 原地更新预览; CSS/JS 自动选中主 HTML 后再切预览
function switchView(view: 'preview' | 'code') {
  if (view === 'preview' && currentFile.value) {
    const e = ext(currentFile.value.name)
    if (!['html', 'htm', 'md'].includes(e)) {
      // CSS/JS 等: 找到主 HTML 文件, 选中它并切预览
      const html = allFiles.value.find(f => ['html', 'htm'].includes(ext(f.name)))
      if (html) {
        currentView.value = 'preview'
        selectFile(html.artifactId, html.name)
        return
      }
    }
  }
  currentView.value = view
}

// 下载当前选中文件
function downloadCurrentFile() {
  const f = currentFile.value
  if (!f) return
  const url = f.url
  const content = (f.artifact.files?.[f.name] as any)?.content
  if (url) {
    const a = document.createElement('a')
    a.href = url; a.download = f.name; a.target = '_blank'
    a.click()
  } else if (content) {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = f.name
    a.click()
    URL.revokeObjectURL(a.href)
  }
}

// 所有产物文件展平为统一列表(带 artifactId + 版本序号, 用于精确点选历史版本)
const allFiles = computed(() => {
  const list: { name: string; size: number; url: string; content?: string; artifact: Artifact; artifactId: number; version: number }[] = []
  props.artifacts.forEach((a, idx) => {
    if (!a.files) return
    const version = idx + 1
    const entries: [string, any][] = Array.isArray(a.files)
      ? (a.files as any[]).map((f: any, i: number) => [f.name || `v${i + 1}`, f])
      : Object.entries(a.files)
    entries.forEach(([name, info]) => {
      list.push({
        name: (info as any).name || name,
        size: (info as any).size || 0,
        url: (info as any).url || '',
        content: (info as any).content || '',
        artifact: a,
        artifactId: a.id,
        version,
      })
    })
  })
  return list
})

const currentFile = computed(() => {
  const list = allFiles.value
  if (selectedName.value) {
    const matched = list.filter(f => f.name === selectedName.value)
    if (matched.length) {
      if (selectedArtifactId.value != null) {
        return matched.find(f => f.artifactId === selectedArtifactId.value) || matched[matched.length - 1]
      }
      return matched[matched.length - 1]  // 仅给 name 时默认最新版本
    }
  }
  return list.find(f => ext(f.name) === 'html') || list[0] || null
})

const mode = computed(() => {
  if (selectedName.value === '__requirement_doc__') return 'requirement'
  return currentFile.value ? previewMode(currentFile.value.name) : 'none'
})

// Markdown 文件预览内容: 优先用内联 content, 否则按 url 拉取
const mdContent = ref<string>('')
watch(
  () => [mode.value, currentFile.value?.name, currentFile.value?.artifactId, currentFile.value?.url],
  async () => {
    if (mode.value !== 'md' || !currentFile.value) { mdContent.value = ''; return }
    const f = currentFile.value
    const inline = (f.artifact.files?.[f.name] as any)?.content
    if (inline) { mdContent.value = inline; return }
    if (f.url) {
      try {
        const resp = await fetch(f.url)
        mdContent.value = await resp.text()
        return
      } catch { /* 拉取失败, 走下方兜底 */ }
    }
    mdContent.value = '(无法加载 Markdown 内容)'
  },
  { immediate: true },
)

// 需求文档下载统一为 .md(走后端 /api/projects/{id}/requirement-doc)
async function downloadReqDoc() {
  if (!props.projectId || !props.requirementDoc) return
  try {
    const resp = await fetch(`/api/projects/${props.projectId}/requirement-doc`)
    if (!resp.ok) return
    const text = await resp.text()
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `requirement_doc_${props.projectId}.md`
    a.click()
    URL.revokeObjectURL(a.href)
  } catch { /* 下载失败忽略 */ }
}

// 暴露给父组件(ChatView): 点击文字产物链接时, 联动右侧预览选中对应文件并打开。
function reset() {
  selectedArtifactId.value = null
  selectedName.value = ''
  currentView.value = 'preview'
}
defineExpose({ selectFile, reset })
</script>

<template>
  <div class="rp-body">
    <!-- 左侧: 需求文档 + 文件树 合并为单列 -->
    <div class="rp-tree">
      <!-- 需求文档(伪目录) -->
      <div v-if="requirementDoc" class="req-tree">
        <div class="tree-head">📋 需求文档</div>
        <div
          class="tree-item"
          :class="{ active: selectedName === '__requirement_doc__' }"
          @click="selectFile('__requirement_doc__')"
        >
          <span class="tree-icon">📄</span>
          <span class="tree-name">{{ requirementDoc.brand?.name || '需求文档' }}.md</span>
          <button class="dl-btn" title="下载 .md" @click.stop="downloadReqDoc()">⬇</button>
        </div>
      </div>

      <!-- 文件树 -->
      <div class="file-tree">
        <div class="tree-head">📁 文件</div>
        <div v-if="uploading" class="upload-banner">
          <span class="spinner-sm"></span> COS 上传中…
          <button class="retry-btn" @click="startRetry(); checkPendingUploads()">重试</button>
        </div>
        <template v-for="f in allFiles" :key="f.artifactId + ':' + f.name">
          <div
            class="tree-item"
            :class="{ active: selectedArtifactId === f.artifactId && selectedName === f.name }"
            @click="selectFile(f.artifactId, f.name)"
          >
            <span class="tree-icon">{{ iconFor(f.name) }}</span>
            <span class="tree-name">{{ f.name }}</span>
            <span class="tree-ver">v{{ f.version }}</span>
            <span class="tree-size" v-if="f.size > 0">{{ sizeKB(f.size) }}</span>
          </div>
        </template>
        <div v-if="!generating && !allFiles.length && !requirementDoc" class="tree-empty">暂无文件</div>
      </div>
    </div>

    <!-- 右侧预览区 -->
    <div class="preview-area">
      <!-- ===== 需求文档(特殊视图) ===== -->
      <template v-if="mode === 'requirement'">
        <div class="pv-toolbar">
          <span class="pv-toolbar-name">📋 {{ requirementDoc?.brand?.name || '需求文档' }}</span>
          <span class="pv-toolbar-spacer"></span>
          <button class="pv-toolbar-btn" title="下载 .md" @click="downloadReqDoc">⬇ 下载</button>
        </div>
        <div class="pv-requirement">
          <div v-if="requirementDoc?.report" class="req-report">
            <MarkdownView :content="requirementDoc.report" />
          </div>
          <div v-else class="req-body">
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
              <div class="req-tags"><span v-for="f in requirementDoc.features" :key="f" class="req-tag">{{ f }}</span></div>
            </div>
            <div v-if="requirementDoc?.design_style" class="req-section">
              <h4>🎨 设计风格</h4>
              <p>{{ requirementDoc.design_style }}
                <span v-if="requirementDoc.color_scheme" class="req-color" :style="{background: requirementDoc.color_scheme.primary}"></span>
              </p>
            </div>
          </div>
        </div>
      </template>

      <!-- ===== 文件预览(统一工具栏 + 代码/预览双视图) ===== -->
      <template v-else-if="currentFile">
        <!-- 顶部工具栏 -->
        <div class="pv-toolbar">
          <span class="pv-toolbar-icon">{{ iconFor(currentFile.name) }}</span>
          <span class="pv-toolbar-name">{{ currentFile.name }}</span>
          <span v-if="currentFile.version" class="pv-toolbar-ver">v{{ currentFile.version }}</span>
          <span class="pv-toolbar-spacer"></span>
          <!-- 视图切换(统一双按钮) -->
          <button class="pv-toolbar-btn" :class="{ active: currentView === 'preview' }" @click="switchView('preview')">👁 预览</button>
          <button class="pv-toolbar-btn" :class="{ active: currentView === 'code' }" @click="switchView('code')">&lt;/&gt; 源码</button>
          <!-- 下载 -->
          <button class="pv-toolbar-btn pv-toolbar-dl" title="下载文件" @click="downloadCurrentFile">⬇ 下载</button>
        </div>

        <!-- 内容体: 根据 currentView 切换预览/源码 -->
        <template v-if="currentView === 'preview'">
          <!-- HTML 预览: 有 COS 直链用 src(纯净), 无直链才用内联 content(srcdoc)。二选一, 绝不混传 -->
          <iframe
            v-if="currentFileIsHTML && currentFile.url"
            class="pv-frame"
            :src="currentFile.url"
            sandbox="allow-scripts allow-forms"
            title="preview"
          ></iframe>
          <iframe
            v-else-if="currentFileIsHTML && !currentFile.url"
            class="pv-frame"
            :srcdoc="((currentFile.artifact?.files?.[currentFile.name || ''] as any)?.content || '')"
            sandbox="allow-scripts allow-forms"
            title="preview"
          ></iframe>
          <!-- Markdown 预览 -->
          <div v-else-if="currentFileIsMD" class="pv-md-body">
            <MarkdownView :content="mdContent" />
          </div>
          <!-- 图片预览 -->
          <div v-else-if="currentFileIsImage" class="pv-body pv-image">
            <img :src="currentFile.url" :alt="currentFile.name" />
          </div>
          <!-- 非预览类型(不应该到这里, 兜底) -->
          <div v-else class="pv-body pv-placeholder">
            <span>该文件类型不支持预览</span>
          </div>
        </template>
        <!-- 源码视图 -->
        <div v-else class="pv-body pv-code">
          <pre><code>{{ (currentFile.artifact.files?.[currentFile.name] as any)?.content || '(二进制文件，无法预览)' }}</code></pre>
        </div>
      </template>

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

/* ---- 左侧: 需求文档 + 文件树 合并为单列, 上下堆叠 ---- */
.rp-tree {
  width: 35%;
  min-width: 160px;
  max-width: 260px;
  border-right: 1px solid var(--border);
  background: var(--panel);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

/* ---- 需求文档(合并进左侧列, 位于文件树上方) ---- */
.req-tree {
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
.req-tree .tree-head {
  color: var(--accent, #6366f1);
}

/* ---- 文件树(需求文档下方) ---- */
.file-tree {
  flex: 1;
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
.tree-ver {
  font-size: 9px; font-weight: 700; color: var(--accent, #6366f1);
  background: var(--accent-light, #eef2ff); border-radius: 8px;
  padding: 0 5px; flex-shrink: 0; margin-right: 2px;
}
.tree-size { font-size: 10px; color: var(--muted); flex-shrink: 0; }
.dl-btn {
  border: none; background: transparent; cursor: pointer; font-size: 13px;
  color: var(--accent, #6366f1); flex-shrink: 0; padding: 0 2px;
  opacity: 0.7; transition: opacity 0.15s;
}
.dl-btn:hover { opacity: 1; }

/* ---- 预览区 ---- */
.preview-area {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  overflow: hidden;
}

/* ---- 统一工具栏 ---- */
.pv-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: var(--bg, #f8fafc);
  border-bottom: 1px solid var(--border, #e2e8f0);
  flex-shrink: 0;
  min-height: 36px;
}
.pv-toolbar-icon { font-size: 14px; flex-shrink: 0; }
.pv-toolbar-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--text, #0f172a);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pv-toolbar-ver {
  font-size: 10px;
  color: var(--brand);
  background: var(--brand-bg);
  border-radius: 10px;
  padding: 1px 6px;
}
.pv-toolbar-spacer { flex: 1; }
.pv-toolbar-btn {
  padding: 3px 10px;
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: var(--radius-sm, 5px);
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  transition: all var(--transition-fast, 0.15s);
  white-space: nowrap;
}
.pv-toolbar-btn:hover { background: var(--hover-bg); color: var(--text); }
.pv-toolbar-btn.active {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.pv-toolbar-btn:disabled { opacity: 0.7; }
.pv-toolbar-dl { color: var(--brand); border-color: var(--brand); }
.pv-toolbar-dl:hover { background: var(--brand-bg); }

/* ---- 内容体 ---- */
.pv-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
*.pv-frame {
  flex: 1;
  min-height: 0;
  width: 100%;
  border: 0;
}
.pv-image {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.pv-image img { max-width: 100%; max-height: 100%; object-fit: contain; }
.pv-code { padding: 12px; overflow: auto; }
.pv-code pre {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text, #334155);
  white-space: pre-wrap;
  word-break: break-all;
}
.pv-md-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  font-size: 13px;
  line-height: 1.6;
}
.pv-code pre {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text, #334155);
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
.spinner {
  width: 24px;
  height: 24px;
  border: 3px solid var(--border);
  border-top-color: var(--brand);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ---- 需求文档预览 ---- */
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
