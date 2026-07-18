<script setup lang="ts">
import type { PlanEvent, ThoughtStep } from '../types'

defineProps<{
  steps: ThoughtStep[]
  plans: PlanEvent[]
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

    <!-- 计划 / 目标特殊节点:大计划作为卡片渲染,区别于普通思考文本 -->
    <div v-for="(p, i) in plans" :key="'plan-' + i" class="plan-card">
      <div class="plan-head">
        <span class="plan-icon">🎯</span>
        <div>
          <div class="plan-title">{{ p.title || '计划' }}</div>
          <div v-if="p.goal" class="plan-goal">{{ p.goal }}</div>
        </div>
      </div>
      <ol v-if="p.steps && p.steps.length" class="plan-steps">
        <li v-for="(s, j) in p.steps" :key="j">{{ s }}</li>
      </ol>
    </div>

    <!-- 分步时间线:每个 agent 节点一步,精准反馈其思考文本 -->
    <ul class="timeline">
      <li
        v-for="s in steps"
        :key="s.stage"
        class="step"
        :class="s.status"
      >
        <span class="dot"></span>
        <div class="step-body">
          <div class="step-label">
            {{ s.label || STAGE_LABELS[s.stage] || s.stage }}
            <span v-if="s.status === 'active'" class="pulse">进行中</span>
            <span v-else-if="s.status === 'done'" class="ok">✓</span>
          </div>
          <pre v-if="s.think" class="think">{{ s.think }}</pre>
          <div v-if="s.stage === 'enter_reviewer' && s.comment" class="review">
            <span :class="s.passed ? 'pass' : 'fail'">{{ s.passed ? '通过' : '未通过' }}</span>
            {{ s.comment }}
          </div>
        </div>
      </li>
    </ul>
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

/* 计划卡片 */
.plan-card {
  border: 1px solid var(--brand2, #c7d2fe);
  background: linear-gradient(180deg, #eef2ff 0%, #fafaff 100%);
  border-radius: 12px;
  padding: 12px 14px;
}
.plan-head {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.plan-icon {
  font-size: 18px;
  line-height: 1.2;
}
.plan-title {
  font-weight: 700;
  font-size: 14px;
  color: var(--brand);
}
.plan-goal {
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
  line-height: 1.5;
}
.plan-steps {
  margin: 10px 0 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.plan-steps li {
  font-size: 13px;
  line-height: 1.5;
  color: #334155;
}

/* 分步时间线 */
.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.step {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  position: relative;
}
.dot {
  flex: none;
  width: 9px;
  height: 9px;
  margin-top: 4px;
  border-radius: 50%;
  background: var(--border);
}
.step.active .dot {
  background: var(--brand);
  box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.15);
}
.step.done .dot {
  background: #22c55e;
}
.step-body {
  flex: 1;
  min-width: 0;
}
.step-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
}
.step.active .step-label {
  color: var(--brand);
}
.pulse {
  font-size: 11px;
  font-weight: 500;
  color: var(--brand);
  background: #eef2ff;
  border-radius: 999px;
  padding: 1px 8px;
  animation: blink 1.2s ease-in-out infinite;
}
.ok {
  color: #22c55e;
}
.think {
  white-space: pre-wrap;
  word-break: break-word;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: #334155;
  max-height: 200px;
  overflow: auto;
  margin: 6px 0 0;
}
.review {
  font-size: 12px;
  margin-top: 6px;
  line-height: 1.5;
  color: #334155;
}
.review .pass {
  color: #16a34a;
  font-weight: 700;
  margin-right: 4px;
}
.review .fail {
  color: #dc2626;
  font-weight: 700;
  margin-right: 4px;
}
@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
</style>
