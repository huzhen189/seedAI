<script setup lang="ts">
import type { ActivityItem, CapabilityNotice } from '../stream/reducer'

withDefaults(defineProps<{
  activities: ActivityItem[]
  capabilityNotices: CapabilityNotice[]
  usage: Record<string, unknown> | null
}>(), {
  usage: null,
})

function compact(value: unknown): string {
  if (typeof value === 'string') return value
  if (value == null) return ''
  const text = JSON.stringify(value)
  return text.length > 180 ? `${text.slice(0, 180)}…` : text
}
</script>

<template>
  <details v-if="activities.length || capabilityNotices.length || usage" class="activity-panel">
    <summary>活动详情 <span>{{ activities.length }} 项</span></summary>
    <div v-if="capabilityNotices.length" class="notices">
      <div v-for="notice in capabilityNotices" :key="notice.id" class="notice">
        <b>{{ notice.feature }} · {{ notice.tier }}</b>
        <span>{{ notice.limitation }}</span>
        <small v-if="notice.upgradeHint">{{ notice.upgradeHint }}</small>
      </div>
    </div>
    <ol v-if="activities.length" class="activity-list">
      <li v-for="item in activities" :key="item.id" :class="[item.kind, item.status]">
        <div class="activity-heading">
          <span class="kind">{{ item.kind === 'task' ? '任务' : '工具' }}</span>
          <b>{{ item.label }}</b>
          <span class="status">{{ item.status }}</span>
        </div>
        <p v-if="item.detail">{{ item.detail }}</p>
        <details v-if="item.input !== undefined || item.output !== undefined" class="io">
          <summary>查看输入/输出</summary>
          <pre v-if="item.input !== undefined">输入：{{ compact(item.input) }}</pre>
          <pre v-if="item.output !== undefined">输出：{{ compact(item.output) }}</pre>
        </details>
      </li>
    </ol>
    <pre v-if="usage" class="usage">用量：{{ compact(usage) }}</pre>
  </details>
</template>

<style scoped>
.activity-panel { margin: 10px 0; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); overflow: hidden; }
.activity-panel > summary { display: flex; justify-content: space-between; padding: 10px 12px; cursor: pointer; color: var(--text); font-size: 13px; font-weight: 700; }
.activity-panel > summary span { color: var(--muted); font-size: 11px; }
.notices { display: flex; flex-direction: column; gap: 6px; padding: 0 10px 8px; }
.notice { display: flex; flex-direction: column; gap: 2px; border-left: 3px solid var(--warn); border-radius: 6px; background: var(--warn-bg); padding: 7px 9px; font-size: 12px; }
.notice b { color: var(--warn); }
.notice span, .notice small { color: var(--text-3); line-height: 1.5; }
.activity-list { display: flex; flex-direction: column; gap: 7px; padding: 0 10px 10px; margin: 0; list-style: none; }
.activity-list li { border: 1px solid var(--border); border-left: 3px solid var(--brand); border-radius: 8px; padding: 8px 9px; }
.activity-list li.failed { border-left-color: var(--err); }
.activity-list li.completed, .activity-list li.succeeded { border-left-color: var(--ok); }
.activity-heading { display: flex; align-items: center; gap: 7px; min-width: 0; }
.kind { border-radius: 999px; background: var(--brand-bg); color: var(--brand); padding: 1px 7px; font-size: 10px; font-weight: 700; }
.activity-heading b { flex: 1; overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.status { color: var(--muted); font-size: 11px; }
p { margin: 5px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.5; }
.io { margin-top: 6px; color: var(--muted); font-size: 11px; }
.io summary { cursor: pointer; }
pre, .usage { margin: 5px 0 0; overflow: auto; white-space: pre-wrap; word-break: break-word; color: var(--text-3); font: inherit; line-height: 1.5; }
.usage { padding: 0 10px 10px; font-size: 11px; }
</style>
