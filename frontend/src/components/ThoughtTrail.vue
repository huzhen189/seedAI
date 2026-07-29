<script setup lang="ts">
import { computed } from 'vue'
import type { PlanEvent, ThoughtStep } from '../types'
import { SOP_ROLES } from '../types'

const props = defineProps<{
  steps: ThoughtStep[]
  plans: PlanEvent[]
  degraded: boolean
  /** 降级原因: model_switch=主模型切换 / timeout=模型超时未产出 / pm=已路由产品经理 */
  degradedReason?: 'model_switch' | 'timeout' | 'pm' | null
  /** 真降级时实际切换的模型序(原始 → 备用), 为空表示未降级或信息未知 */
  switchModelInfo?: string | null
  current: string
  /** 意图识别结果(两级) */
  intent: { level1: string; level2: string }
  /** §9: 当前 SOP 阶段(由 ChatView 按运行中的子任务/计划预览传入) */
  currentRole?: string
}>()

// §9: SOP 四角色进度条(高亮当前阶段)
const sopChain = computed(() =>
  SOP_ROLES.map((r) => ({ ...r, active: r.key === (props.currentRole as string | undefined) })),
)

const INTENT_COLORS: Record<string, string> = {
  learn: '#dbeafe',
  code: '#dcfce7',
  build: '#fef3c7',
  doc: '#f5f3ff',
  translate: '#fdf2f8',
}

const STAGE_LABELS: Record<string, string> = {
  received: '系统已收到你的需求',
  intent_recognized: '已识别意图',
  enter_router: '意图路由 — 识别你的需求类型',
  dispatch: '技能调度 — 加载 AI 能力',
  analyzing: '正在进行意图分析',
  pm_summon: '系统分析到您缺少开发方向，已自动为您召唤产品经理进行分析',
  orchestration: '系统正在对你的需求进行拆分',
  enter_planner: '需求规划 — 拆解任务/制定步骤',
  enter_coder: '代码生成 — 编写构建代码',
  enter_reviewer: '评审校验 — 检查完整性',
  previewing: '投递预览 — 上传预览环境',
  preview: '生成预览 — 产出可预览页面',
  merge: '任务执行完毕，正在进行结果汇总',
  qc_checking: '系统正在核对本次对话生成质量...',
  generating: '正在生成回复中',
  done: '任务执行完毕',
}

// 每个阶段配一个 emoji 图标, 让时间线更直观(对应 ChatView 推送的 stage 名)
const STEP_ICONS: Record<string, string> = {
  received: '✅',
  intent_recognized: '🧭',
  enter_router: '🧭',
  dispatch: '⚡',
  analyzing: '🔍',
  pm_summon: '🧠',
  orchestration: '🧩',
  doc_plan: '📝',
  doc_write: '✍️',
  doc_proofread: '🔧',
  writing: '✍️',
  enter_planner: '🧩',
  enter_coder: '💻',
  enter_reviewer: '🔎',
  previewing: '📤',
  preview: '🌐',
  merge: '📦',
  qc_checking: '⚖️',
  generating: '💬',
  refined: '📋',
  done: '✅',
  degraded_warn: '⚠️',
}
function iconFor(stage: string): string {
  return STEP_ICONS[stage] || '•'
}

// 是否路由到产品经理(PM)进行需求澄清/分析: build/requirement 即 PM 路径
const isPM = computed(() => props.intent?.level1 === 'build' && props.intent?.level2 === 'requirement')
// 降级提示文案: PM 路径用友好叙事, 其余按真实原因区分
const degradedText = computed(() => {
  if (props.degradedReason === 'pm' || (isPM.value && props.degraded)) {
    return '🧠 系统分析到您缺少开发方向，已自动为您召唤产品经理进行分析'
  }
  if (props.degradedReason === 'model_switch') {
    const info = props.switchModelInfo ? `(${props.switchModelInfo})` : ''
    return `⚠ 主模型繁忙，已自动切换备用模型继续生成${info}`
  }
  return '⚠ 模型响应超时，本次未能生成内容'
})

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
    <!-- §9: SOP 四角色进度条(高亮当前阶段) -->
    <div v-if="sopChain.some((r) => r.active)" class="sop-bar">
      <template v-for="(r, i) in sopChain" :key="r.key">
        <span class="sop-node" :class="{ active: r.active }">{{ r.label }}</span>
        <span v-if="i < sopChain.length - 1" class="sop-arrow">→</span>
      </template>
    </div>
    <div
      v-if="intent.level1"
      class="intent-badge"
      :style="{ background: INTENT_COLORS[intent.level1] || '#f1f5f9' }"
    >
      🧠 已识别: {{ intentLabel(intent) }}
    </div>
    <div v-if="degraded" class="badge warn">{{ degradedText }}</div>

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
      <li
        v-for="s in steps"
        :key="s.stage"
        class="step"
        :class="[s.status, s.stage === 'degraded_warn' ? 'warn' : '']"
      >
        <span class="dot"></span>
        <span class="step-icon">{{ iconFor(s.stage) }}</span>
        <div class="step-body">
          <div class="step-label">
            {{ s.label || STAGE_LABELS[s.stage] || s.stage }}
            <span v-if="s.status === 'active'" class="pulse">
              进行中<span class="typing"><i></i><i></i><i></i></span>
            </span>
            <span v-else-if="s.status === 'done' && s.stage !== 'degraded_warn'" class="ok">✓</span>
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
/* §9: SOP 四角色进度条 */
.sop-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.sop-node {
  font-size: 12px;
  font-weight: 600;
  border-radius: 999px;
  padding: 3px 12px;
  background: #f1f5f9;
  color: var(--muted);
  border: 1px solid transparent;
  transition: all 0.25s ease;
}
.sop-node.active {
  background: var(--brand);
  color: #fff;
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.15);
}
.sop-arrow { color: var(--muted); font-size: 13px; }
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
/* 每一步进入时播放入画动画: 新步骤挂载即触发, 形成"一条一条播放"的实时反馈感 */
.step { display: flex; gap: 8px; align-items: flex-start; position: relative; animation: stepIn 0.4s cubic-bezier(0.16, 1, 0.3, 1) both; }
@keyframes stepIn {
  from { opacity: 0; transform: translateY(8px) scale(0.98); }
  to { opacity: 1; transform: none; }
}
.dot { flex: none; width: 9px; height: 9px; margin-top: 4px; border-radius: 50%; background: var(--border); transition: background 0.3s ease; }
.step-icon { flex: none; font-size: 15px; line-height: 1; margin-top: 1px; }
.step.active .dot { background: var(--brand); box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.15); }
.step.done .dot { background: #22c55e; }
/* 降级警告步: 橙色高亮, 区别于常规完成态 */
.step.warn .dot { background: var(--warn); box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.18); }
.step.warn .step-label { color: var(--warn); font-weight: 700; }
.step-body { flex: 1; min-width: 0; }
.step-label { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--muted); }
.step.active .step-label { color: var(--brand); }
.pulse { font-size: 11px; font-weight: 500; color: var(--brand); background: #eef2ff; border-radius: 999px; padding: 1px 8px; display: inline-flex; align-items: center; gap: 4px; animation: blink 1.2s ease-in-out infinite; }
/* 进行中打字指示点 */
.typing { display: inline-flex; gap: 2px; }
.typing i { width: 3px; height: 3px; border-radius: 50%; background: var(--brand); opacity: 0.4; animation: typingDot 1.2s infinite; }
.typing i:nth-child(2) { animation-delay: 0.2s; }
.typing i:nth-child(3) { animation-delay: 0.4s; }
@keyframes typingDot { 0%, 60%, 100% { opacity: 0.3; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-2px); } }
.ok { color: #22c55e; }
.think { white-space: pre-wrap; word-break: break-word; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; line-height: 1.6; color: #334155; max-height: 200px; overflow: auto; margin: 6px 0 0; }
.review { font-size: 12px; margin-top: 6px; line-height: 1.5; color: #334155; }
.review .pass { color: #16a34a; font-weight: 700; margin-right: 4px; }
.review .fail { color: #dc2626; font-weight: 700; margin-right: 4px; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
</style>
