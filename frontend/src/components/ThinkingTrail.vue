<script setup lang="ts">
// ThinkingTrail：单一「思考与执行过程」组件（合并原 StageRail + ActivityPanel + think 流）。
//
// ── 结构（三段式，单/多意图统一，纯聊天也走同一套骨架）─────────────────────────
//   ① 理解意图    S0–S3   token 框(有流才出) → 收流后换成「识别到的意图」中文列表
//   ② 构建网站 / 思考并组织回复
//                 site: S4–S6 ；其它: S4–S8
//                 执行计划列表(每行一个子任务 + 实时状态) + token 框(有流才出)
//   ③ 检查并生成预览  S7–S8（仅 site，纯聊天无此段）
//   ④ 完成        S9      纯终点，不承载任何流；整轮结束后本组件自动折叠
//
// ── token 框规则 ─────────────────────────────────────────────────────────────
//   · 固定高度盒子内滚动（不是 max-height：高度恒定，避免流式期间整个面板反复抖动撑高）
//   · 新内容自动吸底
//   · 所属分组进入 completed 后立即消失（“token 流结束之后 token 框消失”）
//   · 后端 0.2s 合并下发一帧（app/domains/chat/service.py::_EMIT_INTERVAL_S）
//
// 数据全部由后端下发，前端不再自造映射：
//   intents ← S2 stage 事件 / done ；plan ← S4 stage 事件 ；子任务状态 ← S6 task 事件
import { computed, nextTick, ref, watch } from 'vue'
import type { StageId } from '../types/contracts.generated'
import type { CapabilityNotice, IntentInfo, PlanItem, StageView } from '../stream/reducer'

const props = defineProps<{
  stages: Record<StageId, StageView>
  /** LLM 思考过程（think 事件累计） */
  thinking: string
  /** 正文 token 累计（无 think 流的模型下用它作为 token 框内容） */
  response: string
  /** 本轮识别到的意图（中文 label 由后端给） */
  intents: IntentInfo[]
  /** 本轮执行计划 + 子任务实时状态 */
  plan: PlanItem[]
  /** 建站专有流式输出 token 累计（来自 gen_token 事件），用于「构建网站」上方小窗 */
  siteToken: string
  /** 建站专有流式推理累计（来自 gen_think 事件） */
  siteThink: string
  capabilityNotices: CapabilityNotice[]
  usage: Record<string, unknown> | null
  generating: boolean
  /** 本轮是否建站上下文（决定第②段标题与是否出现「检查并生成预览」段） */
  isSiteBuild: boolean
  showDevelopment?: boolean
}>()

// 终态后是否展开回看（默认收起，只留入口）
const expanded = ref(false)

// 开发者面板用的 S0–S9 全量中文名
const STEP_LABELS = computed<Record<string, string>>(() => ({
  S0: props.isSiteBuild ? '接收需求' : '接收消息',
  S1: '加载上下文',
  S2: '理解意图',
  S3: '合并状态',
  S4: '确定路径',
  S5: '检查条件',
  S6: props.isSiteBuild ? '构建网站' : '思考并组织回复',
  S7: '整理结果',
  S8: props.isSiteBuild ? '检查并生成预览' : '生成回复',
  S9: '完成',
}))

type GroupKind = 'understand' | 'execute' | 'preview' | 'finish'
interface GroupDef {
  id: GroupKind
  label: string
  ids: StageId[]
}

// 分组定义：
// - 「准备回复(S5)」与「组织回复(S6–S8)」已合并为单一「思考并组织回复」，不再是两段；
// - 执行段整体前移，紧跟在「理解意图」之后，让计划列表尽早可见；
// - 建站额外保留「检查并生成预览」（S7/S8 是真实的产物校验+预览生成，值得单列）。
const productGroups = computed<GroupDef[]>(() => {
  const understand: GroupDef = { id: 'understand', label: '理解意图', ids: ['S0', 'S1', 'S2', 'S3'] }
  const finish: GroupDef = { id: 'finish', label: '完成', ids: ['S9'] }
  if (props.isSiteBuild) {
    return [
      understand,
      { id: 'execute', label: '构建网站', ids: ['S4', 'S5', 'S6'] },
      { id: 'preview', label: '检查并生成预览', ids: ['S7', 'S8'] },
      finish,
    ]
  }
  return [
    understand,
    { id: 'execute', label: '思考并组织回复', ids: ['S4', 'S5', 'S6', 'S7', 'S8'] },
    finish,
  ]
})

type GroupStatus = 'pending' | 'active' | 'completed' | 'paused' | 'blocked' | 'failed'

const visibleGroups = computed(() => productGroups.value.map((group) => {
  const members = group.ids.map((id) => props.stages[id])
  const active = members.find((item) => item.status === 'active')
  const issue = members.find((item) => ['paused', 'blocked', 'failed'].includes(item.status))
  const completed = members.every((item) => item.status === 'completed')
  const status = (issue?.status || active?.status || (completed ? 'completed' : 'pending')) as GroupStatus
  return {
    ...group,
    status,
    detail: active?.detail || issue?.detail || members.map((item) => item.detail).find(Boolean) || '',
  }
}))

/** 每段的 token 流内容：目前只有执行段(S6)会产出流；理解段预留（S2 若将来接 LLM 会自动生效）。 */
function streamTextOf(kind: GroupKind): string {
  if (kind !== 'execute') return ''
  // 优先展示思考流；模型不吐 think 时退回正文 token，保证「有 token 流的地方都能看到流」。
  return props.thinking || props.response
}

/** 建站专有流内容：gen_think 优先（看模型推理），再退回 gen_token 正文。 */
function siteStreamText(): string {
  return props.siteThink || props.siteToken
}

/** 建站小窗是否可见：仅建站上下文 + 有建站流内容 + 尚在生成中（构建网站段未完成则已收起）。 */
function showSiteStream(status: GroupStatus): boolean {
  if (!props.isSiteBuild) return false
  if (status === 'completed' || status === 'pending') return false
  return siteStreamText().length > 0
}

/**
 * 聊天 token 框可见性：有内容 + 所属段(执行段)尚未完成。
 * 段一旦 completed 立刻收起，避免终态还残留一个空转的流框（用户明确要求「流结束框消失」）。
 */
function showStreamBox(kind: GroupKind, status: GroupStatus): boolean {
  if (kind !== 'execute') return false
  if (status === 'completed' || status === 'pending') return false
  return streamTextOf(kind).length > 0
}

/** 意图列表在理解段收流后展示；单意图 / 多意图统一列表渲染，风格一致。 */
function showIntentList(kind: GroupKind, status: GroupStatus): boolean {
  return kind === 'understand' && status !== 'pending' && props.intents.length > 0
}

/** 执行计划列表：S4 一出计划就展示，纯聊天也有一行（后端补的虚拟 chat 条目）。 */
function showPlanList(kind: GroupKind, status: GroupStatus): boolean {
  return kind === 'execute' && status !== 'pending' && props.plan.length > 0
}

// ── token 框自动吸底 ────────────────────────────────────────────────────────
// 用 Map 存每个段的 DOM ref：段是 v-for 出来的，不能用单一 ref。
const streamBoxes = new Map<string, HTMLElement>()
function bindStreamBox(kind: string, el: unknown): void {
  if (el instanceof HTMLElement) streamBoxes.set(kind, el)
  else streamBoxes.delete(kind)
}
function bindSiteStreamBox(el: unknown): void {
  if (el instanceof HTMLElement) streamBoxes.set('site-stream', el)
  else streamBoxes.delete('site-stream')
}
watch(
  () => [props.thinking, props.response, props.siteThink, props.siteToken],
  async () => {
    await nextTick()
    streamBoxes.forEach((el) => {
      // 仅当用户没有主动上滑回看时才吸底（留 24px 容差）。
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24 + 40
      if (atBottom) el.scrollTop = el.scrollHeight
    })
  },
)

// 已出现的段数（折叠入口文案）
const segmentCount = computed(() => {
  let n = visibleGroups.value.filter((g) => g.status !== 'pending').length
  if (props.intents.length) n++
  if (props.plan.length) n++
  return Math.max(n, 1)
})

// 生成中始终展开；终态后仅当用户点开才显示细节
const showDetails = computed(() => props.generating || expanded.value)

const PLAN_STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  blocked: '已阻断',
}
function planStatusLabel(status: string): string {
  return PLAN_STATUS_LABELS[status] || status
}
function planStatusIcon(status: string): string {
  if (status === 'succeeded') return '✓'
  if (status === 'failed') return '✕'
  if (status === 'blocked') return '!'
  if (status === 'running') return '●'
  return '○'
}
function fmtDuration(ms?: number): string {
  if (!ms || ms <= 0) return ''
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}
function compact(value: unknown): string {
  if (typeof value === 'string') return value
  if (value == null) return ''
  const text = JSON.stringify(value)
  return text.length > 180 ? `${text.slice(0, 180)}…` : text
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
      <div
        v-for="(g, i) in visibleGroups"
        :key="g.id"
        class="tt-group"
        :class="[g.status, { live: generating && g.status === 'active' }]"
      >
        <span class="tt-marker">{{ g.status === 'completed' ? '✓' : g.status === 'active' ? '●' : (i + 1) }}</span>
        <div class="tt-group-body">
          <!-- 建站专有流式小窗：位于「构建网站」分组上方(即"正在为你生成网站…"文案之上)，
               固定高度内滚动，实时展示 LLM 真实生成的 token 流。仅在建站流有内容且未收尾时显示。 -->
          <div
            v-if="g.id === 'execute' && showSiteStream(g.status)"
            class="tt-stream site-stream"
            :ref="(el) => bindSiteStreamBox(el)"
          >
            <div class="tt-stream-head">大模型正在生成站点…</div>
            <pre class="tt-stream-text">{{ siteStreamText() }}</pre>
          </div>

          <div class="tt-group-label">
            {{ g.label }}
            <span v-if="g.status === 'active' && generating" class="tt-pulse">
              进行中<span class="typing"><i></i><i></i><i></i></span>
            </span>
            <span v-else-if="g.status === 'completed'" class="tt-ok">✓</span>
          </div>
          <div v-if="g.detail && g.id !== 'finish'" class="tt-group-detail">{{ g.detail }}</div>

          <!-- token 流框：固定高度内滚动，所属段完成后整体消失 -->
          <div
            v-if="showStreamBox(g.id, g.status)"
            class="tt-stream"
            :ref="(el) => bindStreamBox(g.id, el)"
          >
            <pre class="tt-stream-text">{{ streamTextOf(g.id) }}</pre>
          </div>

          <!-- 理解段：收流后的意图列表（单/多意图统一列表） -->
          <ul v-if="showIntentList(g.id, g.status)" class="tt-list intents">
            <li v-for="(it, idx) in intents" :key="`${it.intent_id}-${idx}`" class="tt-list-row">
              <span class="tt-idx">{{ idx + 1 }}</span>
              <b class="tt-list-label">{{ it.label }}</b>
              <span class="tt-tag" :class="it.domain">{{ it.domain }}</span>
            </li>
          </ul>

          <!-- 执行段：执行计划列表 + 子任务实时状态 -->
          <ul v-if="showPlanList(g.id, g.status)" class="tt-list plan">
            <li
              v-for="(item, idx) in plan"
              :key="item.id || idx"
              class="tt-list-row"
              :class="item.status"
            >
              <span class="tt-idx" :class="item.status">{{ planStatusIcon(item.status) }}</span>
              <b class="tt-list-label">{{ item.label }}</b>
              <span v-if="fmtDuration(item.durationMs)" class="tt-dur">{{ fmtDuration(item.durationMs) }}</span>
              <span class="tt-state" :class="item.status">{{ planStatusLabel(item.status) }}</span>
            </li>
          </ul>
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
          <span v-for="stage in stages" :key="stage.id" :class="stage.status">
            {{ stage.id }} · {{ STEP_LABELS[stage.id] || stage.label }}
          </span>
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
  gap: 9px;
  animation: ttIn 0.4s cubic-bezier(0.16, 1, 0.3, 1) both;
}
@keyframes ttIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

/* ---- 段 ---- */
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

/* ---- token 流框：固定高度 + 内部滚动 ---- */
.tt-stream {
  margin-top: 6px;
  height: 132px;              /* 固定高度：流式期间面板不再上下抖动 */
  overflow-y: auto;
  overscroll-behavior: contain;
  background: var(--brand-bg);
  border: 1px solid var(--brand-border);
  border-radius: 10px;
  padding: 8px 10px;
  /* 顶部渐隐，暗示上方还有内容 */
  mask-image: linear-gradient(180deg, transparent 0, #000 14px);
  -webkit-mask-image: linear-gradient(180deg, transparent 0, #000 14px);
}
.tt-stream::-webkit-scrollbar { width: 6px; }
.tt-stream::-webkit-scrollbar-thumb { background: var(--brand-border); border-radius: 3px; }
.tt-stream-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #0e9b86;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
}
/* 建站专有小窗：在「构建网站」上方，强调"正在生成"，与聊天 token 框区分色调 */
.tt-stream.site-stream {
  background: linear-gradient(180deg, color-mix(in srgb, var(--brand) 12%, var(--surface-1)), var(--surface-2));
  border-color: var(--brand);
}
.tt-stream-head {
  font-size: 11px;
  font-weight: 700;
  color: var(--brand);
  margin-bottom: 4px;
  position: sticky;
  top: -8px;
  background: var(--surface-1);
  padding: 2px 0;
}

/* ---- 列表（意图 / 执行计划共用骨架，保持风格统一）---- */
.tt-list { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.tt-list-row {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface-2); padding: 5px 9px;
}
.tt-list.plan .tt-list-row.running { border-color: var(--brand); background: var(--brand-bg); }
.tt-list.plan .tt-list-row.succeeded { border-left: 3px solid var(--ok); }
.tt-list.plan .tt-list-row.failed { border-left: 3px solid var(--err); }
.tt-list.plan .tt-list-row.blocked { border-left: 3px solid var(--warn); }
.tt-idx {
  flex: 0 0 16px; width: 16px; height: 16px; display: inline-grid; place-items: center;
  border-radius: 50%; background: var(--surface-3); color: var(--muted);
  font-size: 10px; font-weight: 700;
}
.tt-idx.running { background: var(--brand); color: #fff; animation: ttPulse 1.1s ease-in-out infinite; }
.tt-idx.succeeded { background: var(--ok); color: #fff; }
.tt-idx.failed { background: var(--err); color: #fff; }
.tt-idx.blocked { background: var(--warn); color: #fff; }
@keyframes ttPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
.tt-list-label { flex: 1; min-width: 0; font-size: 12px; font-weight: 600; color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tt-tag { font-size: 10px; font-weight: 700; border-radius: 999px; padding: 1px 7px; background: var(--surface-3); color: var(--muted); }
.tt-tag.site { background: var(--brand-bg); color: var(--brand); }
.tt-dur { font-size: 10px; color: var(--muted); }
.tt-state { font-size: 11px; color: var(--muted); flex: 0 0 auto; }
.tt-state.running { color: var(--brand); }
.tt-state.succeeded { color: var(--ok); }
.tt-state.failed { color: var(--err); }
.tt-state.blocked { color: var(--warn); }

/* ---- 能力提示 / 用量 / 开发面板 ---- */
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
