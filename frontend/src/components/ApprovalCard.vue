<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  approval: Record<string, unknown>
  submitting?: boolean
}>()

const emit = defineEmits<{
  decision: [decision: 'approve' | 'reject']
}>()

const approvalId = computed(() => stringValue(props.approval.approval_id))
const action = computed(() => stringValue(props.approval.action) || '待审批操作')
const status = computed(() => stringValue(props.approval.status) || 'pending_first')
const risk = computed(() => stringValue(props.approval.risk_level) || 'high')
const target = computed(() => {
  const value = props.approval.target
  return value && typeof value === 'object' ? JSON.stringify(value) : stringValue(value)
})
const expiresAt = computed(() => stringValue(props.approval.expires_at))
const requiresSecondStep = computed(() => status.value === 'pending_second' || status.value === 'first_confirmed')

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}
</script>

<template>
  <aside class="approval-card" :class="risk">
    <div class="heading">
      <div>
        <span class="eyebrow">需要明确审批</span>
        <h3>{{ action }}</h3>
      </div>
      <span class="risk">{{ risk === 'critical' ? '关键操作' : '高风险操作' }}</span>
    </div>
    <p v-if="target" class="target">目标：{{ target }}</p>
    <p v-if="requiresSecondStep" class="step-notice">此操作需要完成下一次独立确认。</p>
    <p v-if="expiresAt" class="expiry">有效期至 {{ expiresAt }}</p>
    <p v-if="!approvalId" class="invalid">审批编号缺失，无法提交决定。</p>
    <div class="actions">
      <button type="button" class="reject" :disabled="submitting || !approvalId" @click="emit('decision', 'reject')">拒绝</button>
      <button type="button" class="approve" :disabled="submitting || !approvalId" @click="emit('decision', 'approve')">
        {{ submitting ? '提交中…' : '批准操作' }}
      </button>
    </div>
  </aside>
</template>

<style scoped>
.approval-card { margin: 10px 0; border: 1px solid var(--warn-border); border-left: 4px solid var(--warn); border-radius: 12px; padding: 14px; background: var(--warn-bg); color: var(--text); }
.approval-card.critical { border-color: var(--err-border); border-left-color: var(--err); background: var(--err-bg); }
.heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.eyebrow { display: block; color: var(--warn); font-size: 11px; font-weight: 700; letter-spacing: .04em; }
.critical .eyebrow { color: var(--err); }
h3 { margin: 3px 0 0; font-size: 14px; }
.risk { flex: none; border: 1px solid currentColor; border-radius: 999px; color: var(--warn); padding: 2px 8px; font-size: 11px; font-weight: 700; }
.critical .risk { color: var(--err); }
.target, .step-notice, .expiry, .invalid { margin: 9px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.6; word-break: break-word; }
.step-notice { color: var(--warn); font-weight: 600; }
.invalid { color: var(--err); }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 13px; }
button { border-radius: 8px; padding: 7px 12px; font: inherit; font-size: 12px; font-weight: 700; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .55; }
.reject { border: 1px solid var(--border); background: var(--surface-2); color: var(--text-2); }
.approve { border: 1px solid var(--warn); background: var(--warn); color: #fff; }
.critical .approve { border-color: var(--err); background: var(--err); }
</style>
