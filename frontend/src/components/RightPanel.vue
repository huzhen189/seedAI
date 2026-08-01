<script setup lang="ts">
/**
 * 右侧产物面板(M9b)。
 *
 *  v3 产物为「版本列表」(不再内联 files); 实际预览经签名端点按短期签名提供。
 *  本组件只负责: 列出版本 → 选中 → 把 (projectId, artifactId) 交给 PreviewPane
 *  由 PreviewPane 管理签名授权与到期重签(本组件不持有任何签名 URL)。
 *
 *  已移除: v2 的 COS 重传轮询(/api/projects/{id}/retry-upload, 后端已无此端点)。
 */
import { ref, computed, watch, onMounted } from 'vue'
import type { Artifact } from '../types'
import PreviewPane from './PreviewPane.vue'

const props = defineProps<{
  artifacts: Artifact[]
  generating: boolean
  projectId: number | null
}>()

const emit = defineEmits<{ refresh: [] }>()

const previewRef = ref<InstanceType<typeof PreviewPane> | null>(null)
const selectedId = ref<number | null>(null)

const versions = computed(() => props.artifacts)

const selectedVersion = computed<Artifact | null>(() => {
  if (selectedId.value == null) return null
  return versions.value.find((a) => a.id === selectedId.value) ?? null
})

const STATUS_LABELS: Record<string, string> = {
  building: '生成中',
  verified: '已校验',
  preview_ready: '可预览',
  failed: '失败',
  deleted: '已删除',
}

function statusLabel(s?: string): string {
  if (!s) return '未知'
  return STATUS_LABELS[s] ?? s
}

function pickDefault() {
  const list = versions.value
  if (!list.length) {
    selectedId.value = null
    return
  }
  const head = list.find((a) => a.is_head)
  const previewable = list.find((a) => a.previewable)
  selectedId.value = (head ?? previewable ?? list[0]).id
}

function selectVersion(id: number) {
  selectedId.value = id
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

// 生成完成: 选中版本从「不可预览」变为「可预览」(artifact_id 未变) → 主动重签。
watch(
  () => selectedVersion.value?.previewable,
  (now, prev) => {
    if (now && !prev && previewRef.value) previewRef.value.reset()
  },
)

onMounted(() => {
  if (selectedId.value == null) pickDefault()
})

function onRefresh() {
  emit('refresh')
}
</script>

<template>
  <div class="rp-body">
    <!-- 左侧: 版本列表 -->
    <div class="rp-tree">
      <div class="tree-head">
        🗂 产物版本
        <button class="refresh-btn" title="刷新列表" @click="onRefresh">⟳</button>
      </div>
      <div v-if="versions.length === 0" class="tree-empty">
        {{ generating ? '正在生成产物…' : '暂无产物' }}
      </div>
      <div
        v-for="v in versions"
        :key="v.id"
        class="tree-item"
        :class="{ active: selectedId === v.id, dim: !v.previewable }"
        @click="selectVersion(v.id)"
      >
        <span class="tree-icon">🌐</span>
        <span class="tree-name">v{{ v.version }}</span>
        <span v-if="v.is_head" class="tree-badge head">HEAD</span>
        <span v-if="v.is_published" class="tree-badge pub">已发布</span>
        <span class="tree-status">{{ statusLabel(v.status) }}</span>
      </div>
    </div>

    <!-- 右侧: 签名预览 -->
    <div class="preview-area">
      <div class="pv-toolbar">
        <span class="pv-toolbar-icon">🌐</span>
        <span class="pv-toolbar-name">
          {{ selectedVersion ? 'v' + selectedVersion.version : '预览' }}
        </span>
        <span v-if="selectedVersion?.is_head" class="pv-toolbar-ver">HEAD</span>
        <span v-if="generating" class="pv-toolbar-live">● 生成中</span>
        <span class="pv-toolbar-spacer"></span>
        <button class="pv-toolbar-btn" title="刷新列表" @click="onRefresh">⟳ 刷新</button>
      </div>
      <div class="pv-body">
        <PreviewPane
          ref="previewRef"
          :project-id="projectId"
          :artifact-id="selectedId"
          entry="index.html"
          :generating="generating"
        />
        <div v-if="generating && versions.length === 0" class="gen-overlay">
          <div class="spinner"></div>
          <span>AI 正在生成网站…</span>
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

/* ---- 左侧: 版本列表 ---- */
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
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  padding: 8px 10px;
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
.tree-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  cursor: pointer;
  font-size: 12px;
  border-left: 2px solid transparent;
  transition: background 0.15s;
}
.tree-item:hover {
  background: var(--surface-3);
}
.tree-item.active {
  background: var(--brand-bg);
  border-left-color: var(--brand);
  font-weight: 600;
}
.tree-item.dim {
  opacity: 0.55;
}
.tree-icon {
  font-size: 14px;
  flex-shrink: 0;
}
.tree-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
.tree-badge.pub {
  color: var(--accent, #15c4a4);
  background: var(--accent-light, var(--brand-bg));
}
.tree-status {
  font-size: 10px;
  color: var(--muted);
  flex-shrink: 0;
}

/* ---- 右侧: 预览 ---- */
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
.pv-toolbar-live {
  font-size: 10px;
  color: var(--brand);
  font-weight: 600;
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
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
.pv-body {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.gen-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--muted);
  background: var(--surface-2);
  font-size: 13px;
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
</style>
