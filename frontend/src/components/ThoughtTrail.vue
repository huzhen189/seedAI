<script setup lang="ts">
defineProps<{
  stages: string[]
  thinks: string
  degraded: boolean
  current: string
}>()

const STAGE_LABELS: Record<string, string> = {
  enter_router: '路由分发',
  dispatch: '技能调度',
  enter_planner: '规划需求',
  enter_coder: '编写代码',
  enter_reviewer: '评审校验',
  previewing: '投递预览',
  preview: '生成预览',
  done: '完成',
}
</script>

<template>
  <div class="trail">
    <div v-if="degraded" class="badge warn">⚠ 主模型不可用,已降级到备用模型</div>

    <ul class="stages">
      <li v-for="s in stages" :key="s" :class="{ active: s === current }">
        <span class="dot"></span>
        {{ STAGE_LABELS[s] || s }}
      </li>
    </ul>

    <pre v-if="thinks" class="think">{{ thinks }}</pre>
  </div>
</template>

<style scoped>
.trail {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.badge {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  align-self: flex-start;
}
.badge.warn {
  background: #fef3c7;
  color: var(--warn);
}
.stages {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.stages li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--muted);
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel);
}
.stages li.active {
  color: var(--brand);
  border-color: var(--brand2);
  background: #eef2ff;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.6;
}
.think {
  white-space: pre-wrap;
  word-break: break-word;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.6;
  color: #334155;
  max-height: 220px;
  overflow: auto;
  margin: 0;
}
</style>
