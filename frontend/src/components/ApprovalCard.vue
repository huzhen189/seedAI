<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps<{
  approval: Record<string, unknown>
  submitting?: boolean
}>()

const emit = defineEmits<{
  decision: [decision: 'approve' | 'reject']
  expired: []
  reauth: []
}>()

const approvalId = computed(() => stringValue(props.approval.approval_id))
const action = computed(() => stringValue(props.approval.action) || '待审批操作')
const status = computed(() => stringValue(props.approval.status) || 'pending_first')
const risk = computed(() => stringValue(props.approval.risk_level) || 'high')
const step = computed(() => numberValue(props.approval.step) || 1)
const requiresTwoStep = computed(() => step.value > 1)
const reauthRequired = computed(() =>
  props.approval.reauth_required === true
  || props.approval.requires_step_up === true
  || status.value === 'reauth_required',
)
const target = computed(() => {
  const value = props.approval.target
  return value && typeof value === 'object' ? JSON.stringify(value) : stringValue(value)
})

const TERMINAL = new Set(['approved', 'rejected', 'expired', 'invalidated', 'consumed', 'submitted'])
const isInvalidated = computed(() => status.value === 'invalidated')
const isTerminal = computed(() => TERMINAL.has(status.value) && !requiresTwoStep.value)

// 当前段序号: pending_first=1, pending_second=2 … 其余按 step 兜底。
const currentStep = computed(() => {
  if (status.value === 'pending_first') return 1
  if (status.value === 'pending_second') return 2
  return step.value
})

// 有效期倒计时: 到期后禁用并提示重新发起。
const expiresAt = computed(() => stringValue(props.approval.expires_at))
const nowMs = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

const expiresAtMs = computed(() => {
  if (!expiresAt.value) return null
  const t = Date.parse(expiresAt.value)
  return Number.isFinite(t) ? t : null
})

const remainingMs = computed(() => {
  if (expiresAtMs.value == null) return null
  return Math.max(0, expiresAtMs.value - nowMs.value)
})

const isExpiredByTime = computed(() => expiresAtMs.value != null && nowMs.value >= expiresAtMs.value)
const isExpired = computed(() => status.value === 'expired' || isExpiredByTime.value)

const countdownText = computed(() => {
  const r = remainingMs.value
  if (r == null) return ''
  if (r <= 0) return '已过期'
  const totalSec = Math.ceil(r / 1000)
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return m > 0 ? `有效期 ${m} 分 ${s.toString().padStart(2, '0')} 秒` : `有效期 ${s} 秒`
})

const disabled = computed(() =>
  props.submitting
  || !approvalId.value
  || isExpired.value
  || isInvalidated.value
  || isTerminal.value
  || reauthRequired.value,
)

const approveLabel = computed(() => {
  if (!requiresTwoStep.value) return props.submitting ? '提交中…' : '批准操作'
  if (status.value === 'pending_first') return '批准（第 1 步）'
  if (status.value === 'pending_second') return '批准（第 2 步确认）'
  return '批准操作'
})

const stepHint = computed(() => {
  if (!requiresTwoStep.value) return ''
  return `需 ${step.value} 段独立确认 · 第 ${currentStep.value}/${step.value} 步`
})

watch(expiresAt, () => { nowMs.value = Date.now() })

onMounted(() => {
  timer = setInterval(() => {
    nowMs.value = Date.now()
    if (isExpired.value && !emittedExpired.value) {
      emittedExpired.value = true
      emit('expired')
    }
  }, 1000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

const emittedExpired = ref(false)

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}
function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}
</script>

<template>
  <aside class="approval-card" :class="[risk, { expired: isExpired, invalid: isInvalidated, reauth: reauthRequired }]">
    <div class="heading">
      <div>
        <span class="eyebrow">需要明确审批</span>
        <h3>{{ action }}</h3>
      </div>
      <span class="risk">{{ risk === 'critical' ? '关键操作' : '高风险操作' }}</span>
    </div>

    <p v-if="target" class="target">目标：{{ target }}</p>
    <p v-if="stepHint" class="step-notice">{{ stepHint }}</p>

    <p v-if="isExpired" class="banner expired-banner">⚠ 审批已过期，请重新描述意图以重新发起。</p>
    <p v-else-if="isInvalidated" class="banner invalid-banner">⚠ 审批已失效（目标已变化），请重新描述意图。</p>
    <p v-else-if="reauthRequired" class="banner reauth-banner">需要重新认证后再操作。</p>
    <p v-else-if="expiresAt && !isTerminal" class="expiry">{{ countdownText }}</p>
    <p v-if="!approvalId" class="invalid">审批编号缺失，无法提交决定。</p>

    <div v-if="reauthRequired" class="actions">
      <button type="button" class="reject" @click="emit('reauth')">重新登录</button>
    </div>
    <div v-else-if="!isTerminal && !isInvalidated && !isExpired" class="actions">
      <button type="button" class="reject" :disabled="disabled" @click="emit('decision', 'reject')">拒绝</button>
      <button type="button" class="approve" :disabled="disabled" @click="emit('decision', 'approve')">
        {{ approveLabel }}
      </button>
    </div>
  </aside>
</template>

<style scoped>
.approval-card { margin: 10px 0; border: 1px solid var(--warn-border); border-left: 4px solid var(--warn); border-radius: 12px; padding: 14px; background: var(--warn-bg); color: var(--text); }
.approval-card.critical { border-color: var(--err-border); border-left-color: var(--err); background: var(--err-bg); }
.approval-card.expired, .approval-card.invalid { border-left-color: var(--muted); opacity: .92; }
.approval-card.reauth { border-left-color: var(--brand); }
.heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.eyebrow { display: block; color: var(--warn); font-size: 11px; font-weight: 700; letter-spacing: .04em; }
.critical .eyebrow { color: var(--err); }
.reauth .eyebrow { color: var(--brand); }
h3 { margin: 3px 0 0; font-size: 14px; }
.risk { flex: none; border: 1px solid currentColor; border-radius: 999px; color: var(--warn); padding: 2px 8px; font-size: 11px; font-weight: 700; }
.critical .risk { color: var(--err); }
.target, .step-notice, .expiry, .invalid, .banner { margin: 9px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.6; word-break: break-word; }
.step-notice { color: var(--warn); font-weight: 600; }
.critical .step-notice { color: var(--err); }
.expiry { font-variant-numeric: tabular-nums; }
.banner { border-radius: 8px; padding: 7px 10px; font-weight: 600; }
.expired-banner, .invalid-banner { background: var(--err-bg); color: var(--err); }
.reauth-banner { background: var(--brand-bg); color: var(--brand); }
.invalid { color: var(--err); }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 13px; }
button { border-radius: 8px; padding: 7px 12px; font: inherit; font-size: 12px; font-weight: 700; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .55; }
.reject { border: 1px solid var(--border); background: var(--surface-2); color: var(--text-2); }
.approve { border: 1px solid var(--warn); background: var(--warn); color: #fff; }
.critical .approve { border-color: var(--err); background: var(--err); }
.reauth .approve { border-color: var(--brand); background: var(--brand); }
</style>
