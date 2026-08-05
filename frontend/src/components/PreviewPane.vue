<script setup lang="ts">
/**
 * 签名预览 iframe(M9b)。
 *
 * 安全约束(REQ-PREVIEW-001 / SEC-PREVIEW-001):
 *   - iframe 通过「绝对签名 URL」加载产物, 该 URL 落在「不携带平台凭证」的 Origin;
 *   - sandbox 不含 allow-same-origin → 沙箱内容拿到不透明 Origin, 无法读取父页 Cookie/DOM;
 *   - referrerpolicy=no-referrer → 不向产物 Origin 泄露平台页面的 referrer;
 *   - 签名是短期的, 本组件在过期前主动重签, 不把 URL 当作永久字段缓存。
 */
import { ref, watch, onMounted, onUnmounted } from 'vue'
import {
  requestPreviewGrant,
  PREVIEW_REFRESH_LEAD,
  type PreviewGrant,
} from '../api/preview'

const props = defineProps<{
  projectId: number | null
  artifactId: number | null
  entry?: string
  generating?: boolean
}>()

const grant = ref<PreviewGrant | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
let timer: ReturnType<typeof setTimeout> | null = null

function clearTimer() {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
}

function scheduleResign(g: PreviewGrant) {
  clearTimer()
  const remaining = g.expires_at - Math.floor(Date.now() / 1000) - PREVIEW_REFRESH_LEAD
  const delay = Math.max(1000, remaining * 1000)
  timer = setTimeout(() => {
    void refresh()
  }, delay)
}

async function refresh() {
  // 没有选中产物(进入页面/未生成) → 不展示 loading,直接空态。
  // 这是修复「一进来就 loading、永远转圈」的关键:loading 只在真正发请求时才置位。
  if (props.projectId == null || props.artifactId == null) {
    grant.value = null
    error.value = null
    loading.value = false
    return
  }
  loading.value = true
  try {
    const g = await requestPreviewGrant(props.projectId, {
      artifactId: props.artifactId,
      entry: props.entry || 'index.html',
    })
    grant.value = g
    error.value = null
    scheduleResign(g)
  } catch (e) {
    grant.value = null
    error.value = e instanceof Error ? e.message : '预览授权失败'
  } finally {
    loading.value = false
  }
}

// 父组件刷新产物(新版本生成/清除)后调用, 立即重签。
function reset() {
  clearTimer()
  void refresh()
}
defineExpose({ reset })

function openInNewTab() {
  if (grant.value?.url) window.open(grant.value.url, '_blank', 'noopener,noreferrer')
}

watch(
  () => [props.projectId, props.artifactId, props.entry],
  () => {
    clearTimer()
    void refresh()
  },
)

onMounted(() => void refresh())
onUnmounted(() => clearTimer())
</script>

<template>
  <div class="preview">
    <div v-if="error" class="state error">
      <span class="state-icon">⚠</span>
      <span class="state-text">无法预览：{{ error }}</span>
      <button class="retry-btn" @click="refresh()">重试</button>
    </div>
    <div v-else-if="loading" class="state">
      <div class="spinner"></div>
      <span class="state-text">正在获取预览授权…</span>
    </div>
    <div v-else-if="!grant" class="state">
      <span class="state-icon">🌐</span>
      <span class="state-text">暂无可预览的产物</span>
    </div>
    <template v-else>
      <div class="pane-toolbar">
        <span class="pane-badge" :class="{ isolated: grant.isolated_origin }">
          {{ grant.isolated_origin ? '独立沙箱预览' : '同源签名预览' }}
        </span>
        <span class="pane-spacer"></span>
        <button class="pane-btn" title="在新标签打开" @click="openInNewTab">↗ 打开</button>
        <button class="pane-btn" title="重新授权" @click="refresh()">⟳ 刷新</button>
      </div>
      <iframe
        class="frame"
        :src="grant.url"
        sandbox="allow-scripts allow-forms"
        referrerpolicy="no-referrer"
        title="site-preview"
      ></iframe>
    </template>
  </div>
</template>

<style scoped>
.preview {
  position: relative;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--surface-2);
  overflow: hidden;
}
.frame {
  flex: 1;
  min-height: 0;
  width: 100%;
  border: 0;
  background: #fff;
}
.state {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: var(--muted);
  padding: 24px;
  text-align: center;
}
.state.error {
  color: var(--err, #ef4444);
}
.state-icon {
  font-size: 22px;
}
.state-text {
  font-size: 13px;
  line-height: 1.5;
  max-width: 320px;
  word-break: break-word;
}
.retry-btn {
  margin-top: 4px;
  border: 1px solid var(--brand);
  background: transparent;
  color: var(--brand);
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  padding: 4px 14px;
  transition: background 0.15s;
}
.retry-btn:hover {
  background: var(--brand-bg);
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
.pane-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  min-height: 34px;
}
.pane-spacer {
  flex: 1;
}
.pane-badge {
  font-size: 10px;
  font-weight: 700;
  color: var(--accent, #15c4a4);
  background: var(--accent-light, var(--brand-bg));
  border-radius: 10px;
  padding: 2px 8px;
  white-space: nowrap;
}
.pane-badge.isolated {
  color: #f59e0b;
  background: var(--warn-bg, rgba(245, 158, 11, 0.12));
}
.pane-btn {
  padding: 3px 10px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: 6px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  transition: all 0.15s;
}
.pane-btn:hover {
  background: var(--hover-bg);
  color: var(--text);
}
</style>
