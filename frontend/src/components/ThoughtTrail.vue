<script setup lang="ts">
import type { PlanEvent, ThoughtStep } from '../types'

defineProps<{
  steps: ThoughtStep[]
  plans: PlanEvent[]
  degraded: boolean
  current: string
  /** 意图识别结果(两级) */
  intent: { level1: string; level2: string }
}>()

const INTENT_COLORS: Record<string, string> = {
  learn: '#dbeafe',
  code: '#dcfce7',
  build: '#fef3c7',
  doc: '#f5f3ff',
  translate: '#fdf2f8',
}

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

function intentLabel(l: { level1: string; level2: string }): string {
  const l1Map: Record<string, string> = {
    learn: '学习理解', code: '编码实战', build: '建站生成',
    doc: '文档方案', translate: '翻译转换',
  }
  const l2Map: Record<string, string> = {
    explain: '概念解释', debug: '排查报错', compare: '技术对比', casual: '日常闲聊',
    snippet: '函数片段', component: 'UI组件', fix: '修复Bug', refactor: '重构优化',
    page: '单页/落地页', site: '完整网站', modify: '修改已有', game: '互动游戏',
    readme: 'README', tutorial: '教程指南', plan: '方案设计',
    text: '文本翻译', code_lang: '代码翻译',
  }
  return `${l1Map[l.level1] || l.level1} → ${l2Map[l.level2] || l.level2}`
}
</script>

<template>
  <div class="trail">
    <div
      v-if="intent.level1"
      class="intent-badge"
      :style="{ background: INTENT_COLORS[intent.level1] || '#f1f5f9' }"
    >
      🧠 已识别: {{ intentLabel(intent) }}
    </div>
    <div v-if="degraded" class="badge warn">⚠ 主模型不可用,已降级到备用模型</div>

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

    <ul class="timeline">
      <li v-for="s in steps" :key="s.stage" class="step" :class="s.status">
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
.intent-badge {
  padding: 8px 14px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 600;
  color: #1e293b;
  border: 1px solid rgba(0, 0, 0, 0.06);
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

.plan-card {
  border: 1px solid var(--brand2, #c7d2fe);
  background: linear-gradient(180deg, #eef2ff 0%, #fafaff 100%);
  border-radius: 12px;
  padding: 12px 14px;
}
.plan-head { display: flex; gap: 10px; align-items: flex-start; }
.plan-icon { font-size: 18px; line-height: 1.2; }
.plan-title { font-weight: 700; font-size: 14px; color: var(--brand); }
.plan-goal { font-size: 12px; color: var(--muted); margin-top: 2px; line-height: 1.5; }
.plan-steps { margin: 10px 0 0; padding-left: 20px; display: flex; flex-direction: column; gap: 4px; }
.plan-steps li { font-size: 13px; line-height: 1.5; color: #334155; }

.timeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.step { display: flex; gap: 10px; align-items: flex-start; position: relative; }
.dot { flex: none; width: 9px; height: 9px; margin-top: 4px; border-radius: 50%; background: var(--border); }
.step.active .dot { background: var(--brand); box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.15); }
.step.done .dot { background: #22c55e; }
.step-body { flex: 1; min-width: 0; }
.step-label { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--muted); }
.step.active .step-label { color: var(--brand); }
.pulse { font-size: 11px; font-weight: 500; color: var(--brand); background: #eef2ff; border-radius: 999px; padding: 1px 8px; animation: blink 1.2s ease-in-out infinite; }
.ok { color: #22c55e; }
.think { white-space: pre-wrap; word-break: break-word; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; line-height: 1.6; color: #334155; max-height: 200px; overflow: auto; margin: 6px 0 0; }
.review { font-size: 12px; margin-top: 6px; line-height: 1.5; color: #334155; }
.review .pass { color: #16a34a; font-weight: 700; margin-right: 4px; }
.review .fail { color: #dc2626; font-weight: 700; margin-right: 4px; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
</style>
