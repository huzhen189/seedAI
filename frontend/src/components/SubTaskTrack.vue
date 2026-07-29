<script setup lang="ts">
import { computed } from 'vue'
import type { RiskLevel, SubTaskStatus, SubTaskView } from '../types'
import { SOP_ROLES, SKILL_TO_ROLE } from '../types'

const props = defineProps<{
  /** 运行时子任务视图模型列表(顺序与编排总览一致) */
  subtasks: SubTaskView[]
  /** 执行策略: parallel = 全并行; mixed = 分层串行 + 层内并行 */
  strategy?: 'parallel' | 'mixed'
}>()

const emit = defineEmits<{
  /** 用户确认放行某中风险(待确认)子任务 */
  (e: 'confirm', subTaskId: string): void
}>()

const STATUS_META: Record<SubTaskStatus, { icon: string; label: string; cls: string }> = {
  pending: { icon: '○', label: '等待中', cls: 'pending' },
  running: { icon: '⟳', label: '执行中', cls: 'running' },
  done: { icon: '✓', label: '已完成', cls: 'done' },
  failed: { icon: '✕', label: '执行失败', cls: 'failed' },
  blocked: { icon: '⛔', label: '高风险拦截', cls: 'blocked' },
  skipped: { icon: '⤼', label: '待确认', cls: 'skipped' },
  cancelled: { icon: '⏹', label: '已取消', cls: 'cancelled' },
  aborted: { icon: '⛔', label: '连接中断', cls: 'aborted' },
  paused: { icon: '⏸', label: '已暂停', cls: 'paused' },
}

const RISK_META: Record<RiskLevel, { label: string; cls: string }> = {
  high: { label: '高', cls: 'risk-high' },
  medium: { label: '中', cls: 'risk-medium' },
  low: { label: '低', cls: 'risk-low' },
}

function statusOf(s: SubTaskView) {
  return STATUS_META[s.status] || STATUS_META.pending
}
function riskOf(s: SubTaskView) {
  return RISK_META[s.risk] || RISK_META.low
}

// §9: 当前 SOP 阶段(按运行中/最近完成的子任务 skill 映射角色), 供进度条高亮
const currentRole = computed(() => {
  const src =
    props.subtasks.find((s) => s.status === 'running') ||
    props.subtasks.find((s) => s.status === 'done') ||
    props.subtasks.find((s) => s.status === 'failed') ||
    props.subtasks.find((s) => s.status === 'cancelled')
  if (!src) return undefined
  return SKILL_TO_ROLE[src.skill]
})
const sopChain = computed(() =>
  SOP_ROLES.map((r) => ({ ...r, active: r.key === currentRole.value })),
)

// 汇总中: 所有子任务已终态(完成/失败/跳过/取消)但合并结果尚未下发
const merging = computed(() => {
  if (props.subtasks.length === 0) return false
  return props.subtasks.every((s) =>
    s.status === 'done' || s.status === 'failed' || s.status === 'skipped' || s.status === 'cancelled' || s.status === 'blocked',
  )
})
</script>

<template>
  <div class="track">
    <div class="track-head">
      <span class="track-title">🧩 系统正在拆分需求</span>
      <span class="strategy" :class="strategy === 'mixed' ? 'strategy-mixed' : 'strategy-parallel'">
        {{ strategy === 'mixed' ? '分层串行 · 层内并行' : '全部并行' }}
      </span>
      <span class="count">{{ subtasks.length }} 个子任务</span>
    </div>

    <!-- §9: SOP 四角色进度条(高亮当前阶段) -->
    <div class="sop-bar">
      <template v-for="(r, i) in sopChain" :key="r.key">
        <span class="sop-node" :class="{ active: r.active }">{{ r.label }}</span>
        <span v-if="i < sopChain.length - 1" class="sop-arrow">→</span>
      </template>
    </div>

    <!-- 汇总中: 各子任务已收尾, 后端正在合并为最终回复 -->
    <div v-if="merging" class="merge-banner">
      <span class="merge-icon">📦</span>
      <span>任务执行完毕，正在进行结果汇总…</span>
    </div>

    <ul class="lanes">
      <li
        v-for="s in subtasks"
        :key="s.id"
        class="lane"
        :class="[statusOf(s).cls, s.layer !== undefined ? 'layered' : '']"
      >
        <div class="lane-top">
          <span class="status-icon" :class="statusOf(s).cls">{{ statusOf(s).icon }}</span>
          <span class="goal" :title="s.goal">{{ s.goal }}</span>
          <span class="risk" :class="riskOf(s).cls">{{ riskOf(s).label }}风险</span>
        </div>

        <div class="lane-meta">
          <span class="skill-tag">{{ s.skill }}</span>
          <span v-if="strategy === 'mixed' && s.layer !== undefined" class="layer-tag">
            第 {{ s.layer + 1 }} 层
          </span>
          <span v-if="s.dependencies && s.dependencies.length" class="dep-tag">
            依赖 {{ s.dependencies.join(', ') }}
          </span>
          <span class="status-label" :class="statusOf(s).cls">{{ statusOf(s).label }}</span>
        </div>

        <!-- 流式产出预览 -->
        <pre v-if="s.tokens" class="stream">{{ s.tokens }}</pre>
        <!-- 完成摘要 -->
        <div v-else-if="s.result_summary" class="summary">
          {{ s.result_summary }}
          <a
            v-for="(a, i) in s.artifacts"
            :key="i"
            class="artifact"
            :href="a"
            target="_blank"
            rel="noopener"
          >🔗 产物 {{ i + 1 }}</a>
        </div>
        <!-- 失败 / 拦截 / 待确认原因 -->
        <div
          v-else-if="s.fail_reason"
          class="reason"
          :class="statusOf(s).cls"
        >
          {{ s.fail_reason }}
          <button
            v-if="s.status === 'skipped'"
            class="confirm-btn"
            @click="emit('confirm', s.id)"
          >确认执行</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.track {
  border: 1px solid var(--border);
  background: linear-gradient(180deg, #fbfbff 0%, #ffffff 100%);
  border-radius: 14px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.track-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.track-title { font-weight: 700; font-size: 14px; color: var(--brand); }
.strategy {
  font-size: 11px;
  font-weight: 600;
  border-radius: 999px;
  padding: 2px 10px;
}
.strategy-parallel { background: #ecfeff; color: #0e7490; border: 1px solid #a5f3fc; }
.strategy-mixed { background: #f5f3ff; color: #6d28d9; border: 1px solid #ddd6fe; }
.count { font-size: 12px; color: var(--muted); margin-left: auto; }

.lanes { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.merge-banner {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 600; color: #6d28d9;
  background: #f5f3ff; border: 1px solid #ddd6fe; border-radius: 10px;
  padding: 8px 12px;
}
.merge-icon { font-size: 16px; animation: lt-bounce 1.2s ease-in-out infinite; }
@keyframes lt-bounce { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }
.lane {
  border: 1px solid var(--border);
  border-left: 3px solid var(--border);
  border-radius: 12px;
  padding: 10px 12px;
  background: #ffffff;
  transition: border-color 0.3s ease, box-shadow 0.3s ease, transform 0.2s ease;
}
.lane.running { border-left-color: var(--brand); box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.08); }
.lane.done { border-left-color: var(--ok); }
.lane.failed { border-left-color: var(--err); }
.lane.blocked { border-left-color: #7f1d1d; background: #fef2f2; }
.lane.skipped { border-left-color: var(--warn); background: #fffbeb; }
.lane.cancelled { border-left-color: #b91c1c; background: #fef2f2; }
.lane.aborted { border-left-color: #7f1d1d; background: #fef2f2; }
.lane.paused { border-left-color: #64748b; background: #f8fafc; }

/* §9: SOP 四角色进度条 */
.sop-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }
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

.lane-top { display: flex; align-items: center; gap: 8px; }
.status-icon { font-size: 15px; line-height: 1; flex: none; }
.status-icon.running { animation: spin 1s linear infinite; color: var(--brand); }
.status-icon.done { color: var(--ok); }
.status-icon.failed, .status-icon.blocked { color: var(--err); }
.status-icon.skipped { color: var(--warn); }
.status-icon.cancelled, .status-icon.aborted { color: #b91c1c; }
.status-icon.paused { color: var(--muted); }
.goal {
  flex: 1;
  min-width: 0;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.risk { font-size: 11px; font-weight: 700; border-radius: 999px; padding: 1px 8px; flex: none; }
.risk-high { background: #fee2e2; color: #b91c1c; }
.risk-medium { background: #fef3c7; color: var(--warn); }
.risk-low { background: #dcfce7; color: #15803d; }

.lane-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.skill-tag {
  font-size: 11px;
  font-weight: 600;
  background: #eef2ff;
  color: var(--brand);
  border-radius: 6px;
  padding: 1px 8px;
}
.layer-tag { font-size: 11px; color: #6d28d9; background: #f5f3ff; border-radius: 6px; padding: 1px 8px; }
.dep-tag { font-size: 11px; color: var(--muted); background: #f3f4f6; border-radius: 6px; padding: 1px 8px; }
.status-label { font-size: 11px; margin-left: auto; color: var(--muted); }

.stream {
  white-space: pre-wrap;
  word-break: break-word;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: #334155;
  max-height: 140px;
  overflow: auto;
  margin: 8px 0 0;
}
.summary { font-size: 12.5px; line-height: 1.6; color: #334155; margin-top: 8px; }
.artifact {
  display: inline-block;
  margin-left: 8px;
  font-size: 12px;
  color: var(--brand);
  text-decoration: none;
}
.artifact:hover { text-decoration: underline; }
.reason { font-size: 12.5px; line-height: 1.6; margin-top: 8px; }
.reason.blocked { color: #b91c1c; }
.reason.skipped { color: var(--warn); }
.reason.failed { color: var(--err); }
.confirm-btn {
  display: inline-block;
  margin-left: 10px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid var(--warn);
  background: #fffbeb;
  color: var(--warn);
  border-radius: 8px;
  padding: 3px 12px;
  cursor: pointer;
  transition: background 0.2s ease, transform 0.15s ease;
}
.confirm-btn:hover { background: var(--warn); color: #fff; transform: translateY(-1px); }

@keyframes spin { to { transform: rotate(360deg); } }
</style>
