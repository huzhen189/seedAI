<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { ModelInfo } from '../types'

const props = defineProps<{ models: ModelInfo[]; model: string }>()
const emit = defineEmits<{ (e: 'update:model', v: string): void }>()

const open = ref(false)
const root = ref<HTMLElement | null>(null)

// 模型列表：后端未返回时回落到写死的默认项（与旧 select 行为一致，fail-soft）。
const FALLBACK: ModelInfo = {
  id: 'qwen',
  label: '通义千问',
  version: '(默认)',
  desc: '推荐模型 · 综合能力均衡，支持思考模式',
  vendor: '阿里云 · 通义千问',
  speed: '标准',
  context: '128K',
}

const list = computed<ModelInfo[]>(() =>
  props.models.length ? props.models : [FALLBACK],
)

const selected = computed<ModelInfo | null>(() => {
  if (props.models.length === 0) return FALLBACK
  return props.models.find((m) => m.id === props.model) ?? props.models[0]
})

// 当前选中的真实 id（含 fallback 场景）。
const activeId = computed<string>(() =>
  props.models.length === 0 ? 'qwen' : props.model,
)

function speedClass(speed?: string): string {
  if (speed === '极速') return 'speed-ultra'
  if (speed === '快') return 'speed-fast'
  return 'speed-std'
}

function toggle() {
  open.value = !open.value
}

function pick(m: ModelInfo) {
  emit('update:model', m.id)
  open.value = false
}

function onDocClick(e: MouseEvent) {
  if (!open.value || !root.value) return
  if (!root.value.contains(e.target as Node)) open.value = false
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && open.value) {
    open.value = false
    ;(root.value?.querySelector('.ms-trigger') as HTMLElement | null)?.focus()
  }
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="model-selector">
    <button
      class="ms-trigger"
      type="button"
      :class="{ 'is-open': open }"
      :aria-expanded="open"
      aria-haspopup="listbox"
      title="选择执行模型"
      @click.prevent="toggle"
    >
      <span class="ms-dot" :class="speedClass(selected?.speed)"></span>
      <span class="ms-label">{{ selected?.label }}</span>
      <span v-if="selected?.version" class="ms-version">{{ selected.version }}</span>
      <svg class="ms-caret" :class="{ 'caret-up': open }" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>

    <Transition name="ms-pop">
      <div
        v-if="open"
        class="ms-panel glass-surface"
        role="listbox"
        aria-label="模型列表"
      >
        <div class="ms-head">选择执行模型</div>
        <button
          v-for="m in list"
          :key="m.id"
          class="ms-item"
          :class="{ 'is-active': m.id === activeId }"
          role="option"
          :aria-selected="m.id === activeId"
          @click="pick(m)"
        >
          <div class="ms-item-top">
            <span class="ms-item-label">{{ m.label }}</span>
            <span v-if="m.version" class="ms-item-ver">{{ m.version }}</span>
            <span v-if="m.speed" class="ms-badge" :class="speedClass(m.speed)">{{ m.speed }}</span>
            <span v-if="m.id === activeId" class="ms-check" aria-hidden="true">✓</span>
          </div>
          <div class="ms-item-meta">
            <span v-if="m.vendor">{{ m.vendor }}</span>
            <span v-if="m.context" class="ms-context">· {{ m.context }} 上下文</span>
          </div>
          <div v-if="m.desc" class="ms-item-desc">{{ m.desc }}</div>
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.model-selector {
  position: relative;
  display: inline-block;
}

/* —— 触发器 —— */
.ms-trigger {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 280px;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  color: var(--text);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}
.ms-trigger:hover {
  border-color: var(--brand);
  box-shadow: var(--glow-soft);
}
.ms-trigger.is-open {
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-bg);
}
.ms-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ms-version {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--brand);
  background: var(--brand-bg);
  border: 1px solid var(--brand-border);
  padding: 1px 7px;
  border-radius: 999px;
  white-space: nowrap;
}
.ms-caret {
  color: var(--text-3);
  flex-shrink: 0;
  transition: transform var(--transition-fast);
}
.ms-caret.caret-up { transform: rotate(180deg); }

/* 速度状态点 */
.ms-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  background: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-bg);
}
.ms-dot.speed-fast { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-bg); }
.ms-dot.speed-ultra { background: var(--brand-3); box-shadow: 0 0 0 3px var(--violet-bg); }

/* —— 弹出面板 —— */
.ms-panel {
  position: absolute;
  bottom: calc(100% + 10px);
  left: 0;
  z-index: 60;
  width: 320px;
  max-width: 86vw;
  padding: 8px;
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}
.ms-head {
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
  padding: 6px 10px 8px;
}

/* —— 模型卡片 —— */
.ms-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 10px 12px;
  margin-bottom: 4px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text);
  cursor: pointer;
  transition: background-color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
}
.ms-item:last-child { margin-bottom: 0; }
.ms-item:hover {
  background: var(--hover-bg);
  border-color: var(--border);
  transform: translateY(-1px);
}
.ms-item.is-active {
  background: var(--brand-bg-soft);
  border-color: var(--brand-border);
}

.ms-item-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ms-item-label {
  font-size: var(--text-base);
  font-weight: 700;
  color: var(--text);
}
.ms-item-ver {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--brand);
  background: var(--brand-bg);
  border: 1px solid var(--brand-border);
  padding: 1px 7px;
  border-radius: 999px;
}
.ms-badge {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.02em;
  padding: 2px 8px;
  border-radius: 999px;
  color: var(--brand);
  background: var(--brand-bg);
  border: 1px solid var(--brand-border);
}
.ms-badge.speed-fast { color: var(--ok); background: var(--ok-bg); border-color: var(--ok-bg); }
.ms-badge.speed-ultra { color: var(--brand-3); background: var(--violet-bg); border-color: var(--violet-bg); }
.ms-check {
  margin-left: auto;
  font-size: 13px;
  font-weight: 800;
  color: var(--brand);
}
.ms-item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  font-size: var(--text-xs);
  color: var(--text-3);
  flex-wrap: wrap;
}
.ms-context { color: var(--text-4); }
.ms-item-desc {
  margin-top: 5px;
  font-size: var(--text-sm);
  line-height: 1.45;
  color: var(--text-3);
}

/* —— 进场动画 —— */
.ms-pop-enter-active,
.ms-pop-leave-active {
  transition: opacity 0.18s cubic-bezier(0.4, 0, 0.2, 1), transform 0.18s cubic-bezier(0.4, 0, 0.2, 1);
}
.ms-pop-enter-from,
.ms-pop-leave-to {
  opacity: 0;
  transform: translateY(8px) scale(0.98);
}
</style>
