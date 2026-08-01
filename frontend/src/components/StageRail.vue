<script setup lang="ts">
import { computed } from 'vue'
import type { StageId } from '../types/contracts.generated'
import type { StageView } from '../stream/reducer'

const props = defineProps<{
  stages: Record<StageId, StageView>
  showDevelopment?: boolean
}>()

const productStages = [
  { id: 'understand', label: '理解需求', ids: ['S0', 'S1', 'S2', 'S3', 'S4'] as StageId[] },
  { id: 'check', label: '检查条件', ids: ['S5'] as StageId[] },
  { id: 'build', label: '构建网站', ids: ['S6'] as StageId[] },
  { id: 'preview', label: '检查并生成预览', ids: ['S7', 'S8'] as StageId[] },
  { id: 'finish', label: '完成/等待操作', ids: ['S9'] as StageId[] },
]

const visibleStages = computed(() => productStages.map((group) => {
  const members = group.ids.map((id) => props.stages[id])
  const active = members.find((item) => item.status === 'active')
  const issue = members.find((item) => ['paused', 'blocked', 'failed'].includes(item.status))
  const completed = members.every((item) => item.status === 'completed')
  return {
    ...group,
    status: issue?.status || active?.status || (completed ? 'completed' : 'pending'),
    detail: active?.detail || issue?.detail || members.map((item) => item.detail).find(Boolean) || '',
  }
}))

function statusLabel(status: string): string {
  return {
    pending: '未开始',
    active: '进行中',
    completed: '已完成',
    paused: '已暂停',
    blocked: '已阻断',
    failed: '失败',
  }[status] || status
}
</script>

<template>
  <section class="stage-rail" aria-label="任务阶段">
    <div class="rail-heading">
      <span>执行进度</span>
      <span class="rail-caption">五阶段</span>
    </div>
    <ol class="rail-list">
      <li v-for="(stage, index) in visibleStages" :key="stage.id" class="rail-item" :class="stage.status">
        <span class="rail-marker">{{ stage.status === 'completed' ? '✓' : index + 1 }}</span>
        <span class="rail-copy">
          <b>{{ stage.label }}</b>
          <small>{{ stage.detail || statusLabel(stage.status) }}</small>
        </span>
      </li>
    </ol>
    <details v-if="showDevelopment" class="dev-stages">
      <summary>开发阶段 S0–S9</summary>
      <div class="dev-grid">
        <span v-for="stage in stages" :key="stage.id" :class="stage.status">{{ stage.id }} · {{ stage.label }}</span>
      </div>
    </details>
  </section>
</template>

<style scoped>
.stage-rail { margin: 8px 0 14px; padding: 13px 14px; border: 1px solid var(--border); border-radius: 14px; background: linear-gradient(180deg, var(--surface-2), var(--surface-1)); }
.rail-heading { display: flex; align-items: center; justify-content: space-between; color: var(--text); font-size: 13px; font-weight: 700; }
.rail-caption { color: var(--muted); font-size: 11px; font-weight: 600; }
.rail-list { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; padding: 0; margin: 12px 0 0; list-style: none; }
.rail-item { position: relative; display: flex; align-items: flex-start; gap: 7px; min-width: 0; color: var(--muted); }
.rail-item:not(:last-child)::after { position: absolute; left: calc(100% - 3px); top: 10px; width: 8px; height: 1px; background: var(--border); content: ''; }
.rail-marker { display: inline-grid; place-items: center; flex: 0 0 20px; width: 20px; height: 20px; border: 1px solid var(--border); border-radius: 50%; background: var(--surface-3); color: var(--muted); font-size: 11px; font-weight: 700; }
.rail-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.rail-copy b { overflow: hidden; color: inherit; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.rail-copy small { overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.rail-item.active { color: var(--brand); }
.rail-item.active .rail-marker { border-color: var(--brand); background: var(--brand); color: #fff; box-shadow: 0 0 0 3px var(--brand-bg); animation: pulse 1.2s ease-in-out infinite; }
.rail-item.completed { color: var(--ok); }
.rail-item.completed .rail-marker { border-color: var(--ok); background: var(--ok); color: #fff; }
.rail-item.paused, .rail-item.blocked { color: var(--warn); }
.rail-item.paused .rail-marker, .rail-item.blocked .rail-marker { border-color: var(--warn); color: var(--warn); }
.rail-item.failed { color: var(--err); }
.rail-item.failed .rail-marker { border-color: var(--err); color: var(--err); }
.dev-stages { margin-top: 12px; color: var(--muted); font-size: 11px; }
.dev-stages summary { cursor: pointer; }
.dev-grid { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.dev-grid span { border: 1px solid var(--border); border-radius: 999px; padding: 2px 7px; }
.dev-grid .active { border-color: var(--brand); color: var(--brand); }
.dev-grid .completed { border-color: var(--ok); color: var(--ok); }
.dev-grid .failed { border-color: var(--err); color: var(--err); }
@keyframes pulse { 50% { transform: scale(.88); opacity: .75; } }
@media (max-width: 860px) { .rail-list { grid-template-columns: 1fr; gap: 6px; } .rail-item:not(:last-child)::after { display: none; } }
</style>
