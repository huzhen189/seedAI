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

// 聚焦阶段: 优先「正在进行的那一步」; 无进行中则取最后一个「已完成」步骤; 否则取第一个 pending。
// 其余阶段不展开, 仅由底部 mini-dots 压缩呈现, 满足「一次只展示一个阶段、自动覆盖上一个」的诉求。
const focusStage = computed(() => {
  const active = visibleStages.value.find((s) => s.status === 'active')
  if (active) return active
  const done = [...visibleStages.value].reverse().find((s) => s.status === 'completed')
  if (done) return done
  return visibleStages.value[0]
})

const isAllDone = computed(() => visibleStages.value.every((s) => s.status === 'completed'))

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
      <span class="rail-caption">{{ isAllDone ? '已完成' : '当前阶段' }}</span>
    </div>

    <!-- 聚焦卡片: 一次只展示当前(或最近)那一个阶段, 其余阶段在下方 mini-dots 压缩呈现 -->
    <div class="rail-focus" :class="focusStage.status">
      <span class="rail-marker">{{ focusStage.status === 'completed' ? '✓' : '●' }}</span>
      <span class="rail-copy">
        <b>{{ focusStage.label }}</b>
        <small>{{ focusStage.detail || statusLabel(focusStage.status) }}</small>
      </span>
    </div>

    <!-- 迷你进度点: 不打堆展开, 仅用圆点表示各阶段完成态(已完成的打勾、当前高亮) -->
    <div class="rail-dots" role="presentation">
      <span
        v-for="(stage, index) in visibleStages"
        :key="stage.id"
        class="rail-dot"
        :class="stage.status"
        :title="stage.label"
      >
        <i v-if="stage.status === 'completed'">✓</i>
        <i v-else-if="stage.status === 'active'">●</i>
        <i v-else>{{ index + 1 }}</i>
      </span>
    </div>

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
/* 聚焦卡片: 仅展示当前(或最近)一个阶段, 自动覆盖上一个 */
.rail-focus { display: flex; align-items: flex-start; gap: 9px; margin-top: 10px; }
.rail-focus .rail-marker { display: inline-grid; place-items: center; flex: 0 0 22px; width: 22px; height: 22px; border: 1px solid var(--border); border-radius: 50%; background: var(--surface-3); color: var(--muted); font-size: 12px; font-weight: 700; }
.rail-focus .rail-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
.rail-focus .rail-copy b { color: inherit; font-size: 13px; }
.rail-focus .rail-copy small { color: var(--muted); font-size: 11px; }
.rail-focus.active { color: var(--brand); }
.rail-focus.active .rail-marker { border-color: var(--brand); background: var(--brand); color: #fff; box-shadow: 0 0 0 3px var(--brand-bg); animation: pulse 1.2s ease-in-out infinite; }
.rail-focus.completed { color: var(--ok); }
.rail-focus.completed .rail-marker { border-color: var(--ok); background: var(--ok); color: #fff; }
.rail-focus.paused, .rail-focus.blocked { color: var(--warn); }
.rail-focus.paused .rail-marker, .rail-focus.blocked .rail-marker { border-color: var(--warn); color: var(--warn); }
.rail-focus.failed { color: var(--err); }
.rail-focus.failed .rail-marker { border-color: var(--err); color: var(--err); }
/* 迷你进度点: 不打堆展开全部阶段 */
.rail-dots { display: flex; gap: 7px; margin-top: 10px; }
.rail-dot { display: inline-grid; place-items: center; width: 18px; height: 18px; border: 1px solid var(--border); border-radius: 50%; background: var(--surface-3); color: var(--muted); font-size: 10px; font-weight: 700; }
.rail-dot.active { border-color: var(--brand); background: var(--brand); color: #fff; box-shadow: 0 0 0 3px var(--brand-bg); }
.rail-dot.completed { border-color: var(--ok); background: var(--ok); color: #fff; }
.rail-dot.paused, .rail-dot.blocked { border-color: var(--warn); color: var(--warn); }
.rail-dot.failed { border-color: var(--err); color: var(--err); }
.dev-stages { margin-top: 12px; color: var(--muted); font-size: 11px; }
.dev-stages summary { cursor: pointer; }
.dev-grid { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.dev-grid span { border: 1px solid var(--border); border-radius: 999px; padding: 2px 7px; }
.dev-grid .active { border-color: var(--brand); color: var(--brand); }
.dev-grid .completed { border-color: var(--ok); color: var(--ok); }
.dev-grid .failed { border-color: var(--err); color: var(--err); }
@keyframes pulse { 50% { transform: scale(.88); opacity: .75; } }
</style>
