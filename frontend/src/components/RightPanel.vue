<script setup lang="ts">
/**
 * 右侧产物面板(M9b)。
 *
 *  结构(终态):
 *   ├─ 左栏 rp-tree: 当前(HEAD)版本的「生成物文件清单」, 仅展示最新一版, 不再做版本切换。
 *   │   标题后在 tree-head 标注当前版本号 vN + HEAD 角标; 文档(.md/.txt)也列出来但默认不勾选发布。
 *   └─ 右栏 preview-area:
 *        pv-toolbar: 预览/代码 分段切换 + 🔗线上(已发布才显示) + 常驻「发布」按钮。
 *        pv-body:    iframe 预览(HEAD 版本)。
 *        pv-code:    源码查看。
 *
 *  「发布」: 点击 → 二次确认弹窗(列出可发布文件, 自动排除 .md/.txt, 用户可勾选) →
 *            确认后把「编辑的 text + 勾选文件」作为一条 chat 消息发回后端(复用现有 publish 链路 + 审批闸门)。
 *            本组件只负责 UI 与收集, 实际发送由父组件 ChatView 经 SSE 走 site_deploy。
 */
import { ref, computed, watch, onMounted } from 'vue'
import type { Artifact } from '../types'
import PreviewPane from './PreviewPane.vue'
import { listArtifactFiles, fetchPublishFileList, type ArtifactFile } from '../api/projects'

const props = defineProps<{
  artifacts: Artifact[]
  generating: boolean
  projectId: number | null
  publishedUrl?: string | null
}>()

const emit = defineEmits<{
  refresh: []
  /** 用户确认发布: 携带编辑文本 + 勾选文件清单, 交由父组件经 chat 发送给后端。 */
  publish: [payload: { text: string; files: string[] }]
}>()

const previewRef = ref<InstanceType<typeof PreviewPane> | null>(null)
const selectedId = ref<number | null>(null)

// 导航模式: 预览 / 代码。
const mode = ref<'preview' | 'code'>('preview')

// 代码视图状态。
const codeFiles = ref<ArtifactFile[]>([])
const codeLoading = ref(false)
const codeError = ref<string | null>(null)

// 左侧文件清单(展示当前/head 版本文件, 不含内容, 按需加载)。
const treeFiles = ref<ArtifactFile[]>([])
const treeLoading = ref(false)

const versions = computed(() => props.artifacts)

// 当前版本(默认 head; 无 head 取最新一列)。
const selectedVersion = computed<Artifact | null>(() => {
  if (selectedId.value == null) return null
  return versions.value.find((a) => a.id === selectedId.value) ?? null
})

const headVersion = computed<Artifact | null>(() => {
  const head = versions.value.find((a) => a.is_head)
  if (head) return head
  return versions.value.length ? versions.value[0] : null
})

function pickDefault() {
  const head = headVersion.value
  selectedId.value = head ? head.id : null
}

watch(
  () => props.artifacts,
  () => {
    if (selectedId.value == null || !versions.value.some((a) => a.id === selectedId.value)) {
      pickDefault()
    }
  },
  { immediate: true },
)

// 生成完成: 选中版本从「不可预览」变为「可预览」 → 主动重签。
watch(
  () => selectedVersion.value?.previewable,
  (now, prev) => {
    if (now && !prev && previewRef.value) previewRef.value.reset()
  },
)

// 切换到「代码」或换版本时加载源码。
watch(
  [mode, selectedId],
  async ([m, aid]) => {
    if (m === 'code' && aid != null && props.projectId != null) {
      await loadCode(aid)
    }
  },
  { immediate: true },
)

async function loadCode(artifactId: number) {
  if (props.projectId == null) return
  codeLoading.value = true
  codeError.value = null
  try {
    const res = await listArtifactFiles(props.projectId, artifactId, true)
    codeFiles.value = res.files
  } catch (e) {
    codeFiles.value = []
    codeError.value = e instanceof Error ? e.message : '加载源码失败'
  } finally {
    codeLoading.value = false
  }
}

onMounted(() => {
  if (selectedId.value == null) pickDefault()
  void loadTreeFiles()
})

async function loadTreeFiles() {
  if (props.projectId == null || selectedId.value == null) return
  treeLoading.value = true
  try {
    const res = await listArtifactFiles(props.projectId, selectedId.value, false)
    treeFiles.value = res.files
  } catch {
    treeFiles.value = []
  } finally {
    treeLoading.value = false
  }
}

// 当前预览版本切换 / refreshing 时同步刷新左侧清单。
watch(selectedId, () => void loadTreeFiles())

function onRefresh() {
  emit('refresh')
  void loadTreeFiles()
}

function langOf(name: string): string {
  if (name.endsWith('.html') || name.endsWith('.htm')) return 'HTML'
  if (name.endsWith('.css')) return 'CSS'
  if (name.endsWith('.js')) return 'JS'
  if (name.endsWith('.json')) return 'JSON'
  return 'TXT'
}

function openOnline() {
  if (props.publishedUrl) window.open(props.publishedUrl, '_blank', 'noopener,noreferrer')
}

// ------------------------------------------------------------------ 发布弹窗
const showPublish = ref(false)
const publishFiles = ref<{ name: string; size: number; isDoc: boolean; checked: boolean }[]>([])
const publishText = ref('')
const publishLoading = ref(false)
const publishError = ref<string | null>(null)

async function openPublishModal() {
  if (props.projectId == null || headVersion.value == null) return
  showPublish.value = true
  publishLoading.value = true
  publishError.value = null
  publishText.value = `请发布当前网站（版本 v${headVersion.value.version}）。`
  try {
    const files = await fetchPublishFileList(props.projectId, props.artifacts)
    // 文档(.md/.txt)也列出, 但默认不勾选; 其余默认勾选。
    publishFiles.value = files.map((f) => ({
      name: f.name,
      size: f.size,
      isDoc: /\.(md|txt)$/i.test(f.name),
      checked: !/\.(md|txt)$/i.test(f.name),
    }))
  } catch (e) {
    publishFiles.value = []
    publishError.value = e instanceof Error ? e.message : '加载发布文件失败'
  } finally {
    publishLoading.value = false
  }
}

function confirmPublish() {
  const selected = publishFiles.value.filter((f) => f.checked).map((f) => f.name)
  // 没有任何文件被勾选时不发送, 避免误发布空内容。
  if (!selected.length && !publishText.value.trim()) return
  emit('publish', { text: publishText.value.trim() || `发布当前网站版本 v${headVersion.value?.version ?? ''}`, files: selected })
  showPublish.value = false
}

function closePublishModal() {
  showPublish.value = false
}
</script>

<template>
  <div class="rp-body">
    <!-- 左侧: 当前版本文件清单(版本号已标注在 tree-head) -->
    <div class="rp-tree">
      <div class="tree-head">
        <span class="tree-head-title">📁 文件</span>
        <span v-if="headVersion" class="tree-head-ver">v{{ headVersion.version }}</span>
        <span v-if="headVersion?.is_head" class="tree-badge head">HEAD</span>
        <button class="refresh-btn" title="刷新列表" @click="onRefresh">⟳</button>
      </div>
      <div v-if="versions.length === 0" class="tree-empty">
        暂无产物
      </div>
      <div v-else-if="treeLoading" class="tree-empty">
        正在加载文件…
      </div>
      <div v-else-if="!treeFiles.length" class="tree-empty">
        当前版本无可展示文件
      </div>
      <div v-else class="file-list">
        <div
          v-for="f in treeFiles"
          :key="f.name"
          class="file-item"
        >
          <span class="file-icon">📄</span>
          <span class="file-name">{{ f.name }}</span>
          <span class="file-size">{{ f.size }} B</span>
        </div>
      </div>
    </div>

    <!-- 右侧: 预览 / 代码 -->
    <div class="preview-area">
      <div class="pv-toolbar">
        <span class="pv-toolbar-icon">🌐</span>
        <span class="pv-toolbar-name">
          {{ selectedVersion ? 'v' + selectedVersion.version : '预览' }}
        </span>
        <span v-if="selectedVersion?.is_head" class="pv-toolbar-ver">HEAD</span>

        <!-- 预览 / 代码 分段导航(原 pv-toolbar-spacer 位置) -->
        <div class="pv-seg" role="tablist">
          <button
            class="pv-seg-btn"
            :class="{ active: mode === 'preview' }"
            role="tab"
            :aria-selected="mode === 'preview'"
            @click="mode = 'preview'"
          >预览</button>
          <button
            class="pv-seg-btn"
            :class="{ active: mode === 'code' }"
            role="tab"
            :aria-selected="mode === 'code'"
            @click="mode = 'code'"
          >代码</button>
        </div>

        <span class="pv-toolbar-spacer"></span>
        <button
          v-if="publishedUrl"
          class="pv-toolbar-btn online"
          title="打开线上地址"
          @click="openOnline"
        >🔗 线上</button>
        <button
          class="pv-toolbar-btn publish"
          title="发布当前网站"
          @click="openPublishModal"
        >🚀 发布</button>
      </div>

      <!-- 预览模式 -->
      <div v-show="mode === 'preview'" class="pv-body">
        <PreviewPane
          ref="previewRef"
          :project-id="projectId"
          :artifact-id="selectedId"
          entry="index.html"
          :generating="generating"
        />
      </div>

      <!-- 代码模式 -->
      <div v-show="mode === 'code'" class="pv-code">
        <div v-if="codeLoading" class="code-state">
          <div class="spinner"></div>
          <span class="state-text">正在加载源码…</span>
        </div>
        <div v-else-if="codeError" class="code-state">
          <span class="state-icon">⚠</span>
          <span class="state-text">无法加载源码：{{ codeError }}</span>
        </div>
        <div v-else-if="!codeFiles.length" class="code-state">
          <span class="state-icon">📄</span>
          <span class="state-text">该版本暂无可展示的源码</span>
        </div>
        <div v-else class="code-list">
          <div v-for="file in codeFiles" :key="file.name" class="code-file">
            <div class="code-file-head">
              <span class="code-lang" :class="'lang-' + langOf(file.name).toLowerCase()">{{ langOf(file.name) }}</span>
              <span class="code-name">{{ file.name }}</span>
              <span class="code-size">{{ file.size }} B</span>
            </div>
            <pre class="code-pre"><code>{{ file.content }}</code></pre>
          </div>
        </div>
      </div>
    </div>

    <!-- 发布二次确认弹窗 -->
    <div v-if="showPublish" class="modal-mask" @click.self="closePublishModal">
      <div class="modal">
        <div class="modal-head">
          <span>🚀 发布确认</span>
          <button class="modal-close" title="关闭" @click="closePublishModal">✕</button>
        </div>
        <div class="modal-body">
          <p class="modal-tip">
            将发布当前网站版本
            <b v-if="headVersion">v{{ headVersion.version }}</b>。
            可勾选要发布的文件（文档 <code>.md / .txt</code> 默认不勾选，发布时自动排除）。
          </p>
          <div class="modal-text-label">发布说明（将随消息发送给后端）：</div>
          <textarea
            v-model="publishText"
            class="modal-text"
            rows="3"
            placeholder="例如：发布上线，包含首页与样式调整"
          ></textarea>

          <div class="file-pick-head">
            <span>生成物文件</span>
            <span class="file-pick-count">{{ publishFiles.filter((f) => f.checked).length }} / {{ publishFiles.length }} 已选</span>
          </div>
          <div v-if="publishLoading" class="file-pick-state">
            <div class="spinner"></div><span>正在加载文件清单…</span>
          </div>
          <div v-else-if="publishError" class="file-pick-state err">{{ publishError }}</div>
          <div v-else-if="!publishFiles.length" class="file-pick-state">无可发布文件</div>
          <div v-else class="file-pick-list">
            <label
              v-for="f in publishFiles"
              :key="f.name"
              class="pick-item"
              :class="{ doc: f.isDoc }"
            >
              <input type="checkbox" v-model="f.checked" />
              <span class="pick-name">{{ f.name }}</span>
              <span class="pick-size">{{ f.size }} B</span>
              <span v-if="f.isDoc" class="pick-tag">文档</span>
            </label>
          </div>
        </div>
        <div class="modal-foot">
          <button class="modal-btn ghost" @click="closePublishModal">取消</button>
          <button class="modal-btn primary" @click="confirmPublish">确认发布</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rp-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

/* ---- 左侧: 文件清单 ---- */
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
.tree-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  padding: 8px 10px;
  position: sticky;
  top: 0;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  z-index: 1;
}
.tree-head-title { flex-shrink: 0; }
.tree-head-ver {
  font-size: 11px;
  font-weight: 800;
  color: var(--text);
  background: var(--surface-3);
  border-radius: 8px;
  padding: 1px 6px;
}
.tree-badge {
  font-size: 9px;
  font-weight: 700;
  border-radius: 8px;
  padding: 1px 5px;
  flex-shrink: 0;
}
.tree-badge.head {
  color: #fff;
  background: var(--brand);
}
.refresh-btn {
  margin-left: auto;
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: 5px;
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 2px 7px;
  color: var(--muted);
  transition: all 0.15s;
}
.refresh-btn:hover {
  color: var(--brand);
  border-color: var(--brand);
}
.tree-empty {
  padding: 12px 10px;
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
}
.file-list {
  padding: 4px 0;
}
.file-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  border-left: 2px solid transparent;
}
.file-item:hover { background: var(--surface-3); }
.file-item.active {
  background: var(--brand-bg);
  border-left-color: var(--brand);
  font-weight: 600;
}
.file-icon { font-size: 13px; flex-shrink: 0; }
.file-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-size { font-size: 10px; color: var(--muted); flex-shrink: 0; }

/* ---- 右侧: 预览 / 代码 ---- */
.preview-area {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--surface-2);
  overflow: hidden;
}
.pv-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  min-height: 36px;
}
.pv-toolbar-icon {
  font-size: 14px;
  flex-shrink: 0;
}
.pv-toolbar-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pv-toolbar-ver {
  font-size: 10px;
  color: #fff;
  background: var(--brand);
  border-radius: 10px;
  padding: 1px 6px;
}
/* 预览 / 代码 分段导航 */
.pv-seg {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
  flex-shrink: 0;
  margin-left: 4px;
}
.pv-seg-btn {
  border: 0;
  background: var(--surface-2);
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 12px;
  cursor: pointer;
  transition: all 0.15s;
  line-height: 1.4;
}
.pv-seg-btn + .pv-seg-btn {
  border-left: 1px solid var(--border);
}
.pv-seg-btn.active {
  background: var(--brand);
  color: #fff;
}
.pv-toolbar-spacer {
  flex: 1;
}
.pv-toolbar-btn {
  padding: 3px 10px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: 5px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  transition: all 0.15s;
  white-space: nowrap;
}
.pv-toolbar-btn:hover {
  background: var(--hover-bg);
  color: var(--text);
}
.pv-toolbar-btn.online {
  color: var(--accent, #15c4a4);
  border-color: var(--accent, #15c4a4);
}
.pv-toolbar-btn.online:hover {
  background: var(--accent-light, var(--brand-bg));
}
.pv-toolbar-btn.publish {
  color: #fff;
  background: var(--brand);
  border-color: var(--brand);
}
.pv-toolbar-btn.publish:hover {
  filter: brightness(1.08);
}
.pv-body {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  /* 关键: 用 block 而非 flex。子组件 .preview 是 height:100% 的块级,
     在 block 父下自动填满整宽;若用 flex(row), 子块会塌缩到 iframe 内禀宽度,
     导致"预览没占满"。高度由 flex:1 在列父 .preview-area 中稳住。 */
  display: block;
}

/* ---- 代码视图 ---- */
.pv-code {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: #0f172a;
}
.code-state {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: var(--muted);
  padding: 24px;
  text-align: center;
}
.state-icon { font-size: 22px; }
.state-text { font-size: 13px; line-height: 1.5; max-width: 320px; word-break: break-word; }
.code-list { padding: 8px 10px 24px; }
.code-file {
  margin-bottom: 10px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 8px;
  overflow: hidden;
  background: #0b1220;
}
.code-file-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: rgba(148, 163, 184, 0.08);
  border-bottom: 1px solid rgba(148, 163, 184, 0.18);
}
.code-lang {
  font-size: 9px;
  font-weight: 700;
  border-radius: 6px;
  padding: 1px 6px;
  background: #1e293b;
  color: #94a3b8;
  flex-shrink: 0;
}
.code-lang.lang-html { color: #fb923c; }
.code-lang.lang-css { color: #38bdf8; }
.code-lang.lang-js { color: #facc15; }
.code-lang.lang-json { color: #a78bfa; }
.code-name {
  font-size: 11px;
  color: #cbd5e1;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.code-size { font-size: 10px; color: #64748b; flex-shrink: 0; }
.code-pre {
  margin: 0;
  padding: 10px 12px;
  max-height: 360px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.55;
  color: #e2e8f0;
  white-space: pre-wrap;
  word-break: break-word;
}
.spinner {
  width: 28px;
  height: 28px;
  border: 3px solid var(--border);
  border-top-color: var(--brand);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 发布弹窗 ---- */
.modal-mask {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
  backdrop-filter: blur(2px);
}
.modal {
  width: min(520px, 92%);
  max-height: 88%;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.4);
  overflow: hidden;
}
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.modal-close {
  border: 0;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: 6px;
}
.modal-close:hover { background: var(--surface-3); color: var(--text); }
.modal-body {
  padding: 14px;
  overflow-y: auto;
}
.modal-tip {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.modal-tip code {
  background: var(--surface-3);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 11px;
}
.modal-tip b { color: var(--brand); }
.modal-text-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  margin-bottom: 6px;
}
.modal-text {
  width: 100%;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-1);
  color: var(--text);
  font-size: 12.5px;
  font-family: inherit;
  padding: 8px 10px;
  line-height: 1.5;
}
.modal-text:focus {
  outline: none;
  border-color: var(--brand);
}
.file-pick-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 14px 0 6px;
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
}
.file-pick-count { color: var(--brand); }
.file-pick-state {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  font-size: 12px;
  color: var(--muted);
}
.file-pick-state.err { color: var(--err, #ef4444); }
.file-pick-list {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow-y: auto;
  max-height: 220px;
}
.pick-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  border-bottom: 1px solid var(--border);
}
.pick-item:last-child { border-bottom: 0; }
.pick-item:hover { background: var(--surface-3); }
.pick-item.doc { color: var(--muted); }
.pick-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
}
.pick-size { font-size: 10px; color: var(--muted); flex-shrink: 0; }
.pick-tag {
  font-size: 9px;
  font-weight: 700;
  color: var(--muted);
  background: var(--surface-3);
  border-radius: 6px;
  padding: 1px 5px;
  flex-shrink: 0;
}
.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 14px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
.modal-btn {
  padding: 7px 18px;
  border-radius: 8px;
  font-size: 12.5px;
  font-weight: 700;
  cursor: pointer;
  border: 1px solid var(--border);
  transition: all 0.15s;
}
.modal-btn.ghost {
  background: var(--surface-2);
  color: var(--text-secondary);
}
.modal-btn.ghost:hover { background: var(--surface-3); }
.modal-btn.primary {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
.modal-btn.primary:hover { filter: brightness(1.08); }
</style>
