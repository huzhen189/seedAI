<script setup lang="ts">
// ThinkingTrail：合并原 StageRail（阶段进度）+ ActivityPanel（工具活动）+ think 流（思考过程）
// 为 WorkBuddy 式「逐段追加展示，终态后折叠可回看」的单一组件。
//
// 行为：
// - 生成中(generating=true)：实时逐段追加展示当前阶段、思考片段、工具活动；
//   "一段一段"播放，最后一条高亮，形成持续反馈感。
// - 终态后(generating=false)：整块折叠为「🧠 查看思考过程（N 段）」入口，可点开回看（A 方案）。
//
// 文案按意图区分：isSiteBuild=true 时走「理解需求 / 构建网站 / 生成预览」等建站叙事；
// 否则走中性文案（闲聊/问答只显示「理解中 / 思考中 / 回复完成」），杜绝"闲聊也被说成在建设中"。
import { computed, ref } from 'vue'
import type { StageId } from '../types/contracts.generated'
import type { ActivityItem, CapabilityNotice, StageView } from '../stream/reducer'

const props = defineProps<{
  stages: Record<StageId, StageView>
  activities: ActivityItem[]
  thinking: string
  capabilityNotices: CapabilityNotice[]
  usage: Record<string, unknown> | null
  generating: boolean
  /** 本轮是否建站上下文（建站时才用"构建网站/生成预览"等叙事文案） */
  isSiteBuild: boolean
  showDevelopment?: boolean
}>()

// 终态后是否展开回看（默认收起，只留入口）
const expanded = ref(false)

// 建站叙事文案 vs 中性文案（闲聊/问答）
const STEP_LABELS = computed(() => {
  if (props.isSiteBuild) {
    return {
      S0: '接收需求', S1: '加载上下文', S2: '理解需求', S3: '合并状态', S4: '确定路径',
      S5: '检查条件', S6: '构建网站', S7: '整理结果', S8: '检查并生成预览', S9: '完成归档',
    }
  }
  return {
    S0: '接收消息', S1: '加载上下文', S2: '理解意图', S3: '合并状态', S4: '确定路径',
    S5: '检查条件', S6: '思考并组织回复', S7: '整理结果', S8: '生成回复', S9: '完成',
  }
})

const productGroups = computed(() => {
  if (props.isSiteBuild) {
    return [
      { id: 'understand', label: '理解需求', ids: ['S0', 'S1', 'S2', 'S3', 'S4'] as StageId[] },
      { id: 'check', label: '检查条件', ids: ['S5'] as StageId[] },
      { id: 'build', label: '构建网站', ids: ['S6'] as StageId[] },
      { id: 'preview', label: '检查并生成预览', ids: ['S7', 'S8'] as StageId[] },
      { id: 'finish', label: '完成/等待操作', ids: ['S9'] as StageId[] },
    ]
  }
  return [
    { id: 'understand', label: '理解意图', ids: ['S0', 'S1', 'S2', 'S3', 'S4'] as StageId[] },
    { id: 'check', label: '准备回复', ids: ['S5'] as StageId[] },
    { id: 'respond', label: '组织回复', ids: ['S6', 'S7', 'S8'] as StageId[] },
    { id: 'finish', label: '完成', ids: ['S9'] as StageId[] },
  ]
})

const visibleGroups = computed(() => productGroups.value.map((group) => {
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

// 已出现的阶段段数（用于"一段一段"计数与折叠入口文案）
const segmentCount = computed(() => {
  let n = 0
  visibleGroups.value.forEach((g) => { if (g.status !== 'pending') n++ })
  if (props.thinking) n++
  if (props.activities.length) n++
  return Math.max(n, 1)
})

// 是否整块显示：生成中始终展开；终态后仅当展开回看时显示细节
const showDetails = computed(() => props.generating || expanded.value)

const TOOL_ICONS: Record<string, string> = {
  web_search: '🔍', cos_upload: '📤', fetch_url: '🌐', rag_retrieve: '🧠',
  image_generate: '🎨', browser_screenshot: '📸', html_validate: '✅', file_io: '📁',
}
function iconForTool(name: string): string {
  return TOOL_ICONS[name] || '🔧'
}
function compact(value: unknown): string {
  if (typeof value === 'string') return value
  if (value == null) return ''
  const text = JSON.stringify(value)
  return text.length > 180 ? `${text.slice(0, 180)}…` : text
}
function statusLabel(status: string): string {
  return ({
    pending: '未开始', active: '进行中', completed: '已完成',
    paused: '已暂停', blocked: '已阻断', failed: '失败',
  } as Record<string, string>)[status] || status
}
</script>

<template>
  <section class="thinking-trail" :class="{ collapsed: !showDetails }" aria-label="思考与执行过程">
    <button class="tt-head" type="button" @click="expanded = !expanded" :disabled="generating">
      <span class="tt-title">{{ generating ? '思考与执行中…' : '🧠 查看思考过程' }}</span>
      <span class="tt-meta">
        <template v-if="generating">实时</template>
        <template v-else>{{ segmentCount }} 段 · {{ expanded ? '收起 ▴' : '展开 ▾' }}</template>
      </span>
    </button>

    <div v-if="showDetails" class="tt-body">
      <!-- 阶段逐段 -->
      <div
        v-for="(g, i) in visibleGroups"
        :key="g.id"
        class="tt-group"
        :class="[g.status, { live: generating && g.status === 'active' }]"
      >
        <span class="tt-marker">{{ g.status === 'completed' ? '✓' : g.status === 'active' ? '●' : (i + 1) }}</span>
        <div class="tt-group-body">
          <div class="tt-group-label">
            {{ g.label }}
            <span v-if="g.status === 'active' && generating" class="tt-pulse">进行中<span class="typing"><i></i><i></i><i></i></span></span>
            <span v-else-if="g.status === 'completed'" class="tt-ok">✓</span>
          </div>
          <div v-if="g.detail" class="tt-group-detail">{{ g.detail }}</div>
        </div>
      </div>

      <!-- 思考流（LLM think），逐段追加在阶段之后 -->
      <div v-if="thinking" class="tt-think">
        <span class="tt-ico">💡</span>
        <pre class="tt-think-body">{{ thinking }}</pre>
      </div>

      <!-- 工具/任务活动（建站等会用到；闲聊通常为空，自动不显示） -->
      <div v-if="activities.length" class="tt-activities">
        <div
          v-for="item in activities"
          :key="item.id"
          class="tt-act"
          :class="[item.kind, item.status]"
        >
          <span class="tt-ico">{{ iconForTool(item.label) }}</span>
          <div class="tt-act-body">
            <div class="tt-act-head">
              <span class="tt-kind">{{ item.kind === 'task' ? '任务' : '工具' }}</span>
              <b>{{ item.label }}</b>
              <span class="tt-status" :class="item.status">{{ statusLabel(item.status) }}</span>
            </div>
            <p v-if="item.detail" class="tt-act-detail">{{ item.detail }}</p>
          </div>
        </div>
      </div>

      <!-- 能力限制提示 -->
      <div v-if="capabilityNotices.length" class="tt-notices">
        <div v-for="notice in capabilityNotices" :key="notice.id" class="tt-notice">
          <b>{{ notice.feature }} · {{ notice.tier }}</b>
          <span>{{ notice.limitation }}</span>
        </div>
      </div>

      <pre v-if="usage" class="tt-usage">用量：{{ compact(usage) }}</pre>

      <details v-if="showDevelopment" class="tt-dev">
        <summary>开发阶段 S0–S9</summary>
        <div class="tt-dev-grid">
          <span v-for="stage in stages" :key="stage.id" :class="stage.status">{{ stage.id }} · {{ STEP_LABELS[stage.id] || stage.label }}</span>
        </div>
      </details>
    </div>
  </section>
</template>

<style scoped>
.thinking-trail {
  margin: 8px 0 12px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: linear-gradient(180deg, var(--surface-2), var(--surface-1));
  overflow: hidden;
}
.tt-head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  text-align: left;
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 9px 13px;
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
}
.tt-head:disabled { cursor: default; }
.tt-meta { color: var(--muted); font-size: 11px; font-weight: 600; }
.tt-body {
  padding: 4px 13px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  animation: ttIn 0.4s cubic-bezier(0.16, 1, 0.3, 1) both;
}
@keyframes ttIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

/* 阶段段：逐段追加，live 段高亮 */
.tt-group { display: flex; gap: 9px; align-items: flex-start; }
.tt-marker {
  flex: 0 0 20px; width: 20px; height: 20px; display: inline-grid; place-items: center;
  border: 1px solid var(--border); border-radius: 50%; background: var(--surface-3);
  color: var(--muted); font-size: 11px; font-weight: 700;
}
.tt-group.active .tt-marker { border-color: var(--brand); background: var(--brand); color: #fff; box-shadow: 0 0 0 3px var(--brand-bg); }
.tt-group.completed .tt-marker { border-color: var(--ok); background: var(--ok); color: #fff; }
.tt-group.paused .tt-marker, .tt-group.blocked .tt-marker { border-color: var(--warn); color: var(--warn); }
.tt-group.failed .tt-marker { border-color: var(--err); color: var(--err); }
.tt-group-body { flex: 1; min-width: 0; }
.tt-group-label { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--text); }
.tt-group.active .tt-group-label { color: var(--brand); }
.tt-group.completed .tt-group-label { color: var(--text-2); }
.tt-group-detail { font-size: 12px; color: var(--muted); line-height: 1.5; margin-top: 2px; }
.tt-ok { color: var(--ok); font-size: 11px; }
.tt-pulse { font-size: 11px; font-weight: 500; color: var(--brand); background: var(--brand-bg); border-radius: 999px; padding: 1px 8px; display: inline-flex; align-items: center; gap: 4px; }
.typing { display: inline-flex; gap: 2px; }
.typing i { width: 3px; height: 3px; border-radius: 50%; background: var(--brand); opacity: 0.4; animation: typingDot 1.2s infinite; }
.typing i:nth-child(2) { animation-delay: 0.2s; }
.typing i:nth-child(3) { animation-delay: 0.4s; }
@keyframes typingDot { 0%, 60%, 100% { opacity: 0.3; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-2px); } }

/* 思考流 */
.tt-think { display: flex; gap: 9px; align-items: flex-start; background: var(--brand-bg); border: 1px solid var(--brand-border); border-radius: 10px; padding: 8px 10px; }
.tt-ico { flex: 0 0 auto; font-size: 14px; line-height: 1.2; margin-top: 1px; }
.tt-think-body { margin: 0; flex: 1; min-width: 0; font-size: 12px; line-height: 1.55; color: #0e9b86; white-space: pre-wrap; word-break: break-word; max-height: 26vh; overflow-y: auto; }

/* 活动 */
.tt-activities { display: flex; flex-direction: column; gap: 6px; }
.tt-act { display: flex; gap: 9px; align-items: flex-start; border: 1px solid var(--border); border-left: 3px solid var(--brand); border-radius: 8px; padding: 7px 9px; background: var(--surface-2); }
.tt-act.failed { border-left-color: var(--err); }
.tt-act.completed, .tt-act.succeeded { border-left-color: var(--ok); }
.tt-act-body { flex: 1; min-width: 0; }
.tt-act-head { display: flex; align-items: center; gap: 7px; min-width: 0; }
.tt-kind { border-radius: 999px; background: var(--brand-bg); color: var(--brand); padding: 1px 7px; font-size: 10px; font-weight: 700; }
.tt-act-head b { flex: 1; overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.tt-status { font-size: 11px; color: var(--muted); }
.tt-status.completed, .tt-status.succeeded { color: var(--ok); }
.tt-status.failed { color: var(--err); }
.tt-act-detail { margin: 4px 0 0; font-size: 12px; color: var(--text-3); line-height: 1.5; }

/* 能力提示 */
.tt-notices { display: flex; flex-direction: column; gap: 6px; }
.tt-notice { display: flex; flex-direction: column; gap: 2px; border-left: 3px solid var(--warn); border-radius: 6px; background: var(--warn-bg); padding: 6px 9px; font-size: 12px; }
.tt-notice b { color: var(--warn); }
.tt-notice span { color: var(--text-3); line-height: 1.5; }
.tt-usage { margin: 0; color: var(--muted); font-size: 11px; }

.tt-dev { margin-top: 4px; color: var(--muted); font-size: 11px; }
.tt-dev summary { cursor: pointer; }
.tt-dev-grid { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.tt-dev-grid span { border: 1px solid var(--border); border-radius: 999px; padding: 2px 7px; }
.tt-dev-grid .active { border-color: var(--brand); color: var(--brand); }
.tt-dev-grid .completed { border-color: var(--ok); color: var(--ok); }
.tt-dev-grid .failed { border-color: var(--err); color: var(--err); }
</style>
