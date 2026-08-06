import type { StageId, StreamEvent } from '../types/contracts.generated'

export type UiStageStatus = 'pending' | 'active' | 'completed' | 'paused' | 'failed' | 'blocked'

export interface StageView {
  id: StageId
  status: UiStageStatus
  label: string
  detail: string
}

export interface ActivityItem {
  id: string
  kind: 'task' | 'tool'
  label: string
  status: string
  detail: string
  input?: unknown
  output?: unknown
}

export interface IntentInfo {
  domain: string
  intent_id: string
  /** 后端下发的中文短语（唯一真相源在 backend/app/core/intent_labels.py） */
  label: string
  speechAct: string
  executable: boolean
}

/** 执行计划条目：来自 S4 stage 事件的 plan，状态由 S6 的 task 事件实时回填。 */
export interface PlanItem {
  id: string
  domain: string
  intentId: string
  label: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'blocked'
  durationMs?: number
}

export interface CapabilityNotice {
  id: string
  feature: string
  tier: string
  limitation: string
  upgradeHint: string
}

export interface StreamUiState {
  streamId: string | null
  turnId: string | null
  traceId: string | null
  lastSeq: number
  seenEventKeys: Set<string>
  pendingBySeq: Map<number, StreamEvent>
  stages: Record<StageId, StageView>
  activities: ActivityItem[]
  response: string
  /** LLM 思考过程实时累计(来自 think 事件), 前端折叠展示 */
  thinking: string
  /**
   * 建站专有流（区别于聊天流）：来自独立事件 gen_token/gen_think。
   * LLM 真实生成站点时的逐块输出，用于「构建网站」分组上方的小窗实时滚动展示。
   * 刻意与聊天 response/thinking 隔离，避免建站正文污染助手回复气泡。
   */
  siteToken: string
  siteThink: string
  attemptOutputs: string[]
  state: Record<string, unknown>
  stateVersion: number
  approval: Record<string, unknown> | null
  suspended: Record<string, unknown> | null
  usage: Record<string, unknown> | null
  capabilityNotices: CapabilityNotice[]
  reconnect: Record<string, unknown> | null
  error: Record<string, unknown> | null
  done: Record<string, unknown> | null
  /**
   * 本轮真实意图。S2 出栈时即由 stage 事件下发（不再等到 done），
   * 前端「理解意图」token 框收流后立刻换成这张中文列表；done 会再覆盖一次做终态对账。
   */
  intents: IntentInfo[]
  /** 本轮执行计划（S4 下发 + S6 task 事件回填状态 + done 终态对账）。 */
  plan: PlanItem[]
}

export interface ReduceResult {
  applied: boolean
  gapAfter: number | null
}

const STAGES: StageId[] = ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9']

const STAGE_LABELS: Record<StageId, string> = {
  S0: '接收请求',
  S1: '加载上下文',
  S2: '理解需求',
  S3: '合并状态',
  S4: '确定路径',
  S5: '检查条件',
  S6: '构建网站',
  S7: '整理结果',
  S8: '检查预览',
  S9: '完成归档',
}

export function createStreamUiState(): StreamUiState {
  const stages = {} as Record<StageId, StageView>
  for (const id of STAGES) {
    stages[id] = { id, status: 'pending', label: STAGE_LABELS[id], detail: '' }
  }
  return {
    streamId: null,
    turnId: null,
    traceId: null,
    lastSeq: 0,
    seenEventKeys: new Set<string>(),
    pendingBySeq: new Map<number, StreamEvent>(),
    stages,
    activities: [],
    response: '',
    thinking: '',
    siteToken: '',
    siteThink: '',
    attemptOutputs: [],
    state: {},
    stateVersion: 0,
    approval: null,
    suspended: null,
    usage: null,
    capabilityNotices: [],
    reconnect: null,
    error: null,
    done: null,
    intents: [],
    plan: [],
  }
}

export function resetStreamUiState(target: StreamUiState): void {
  const next = createStreamUiState()
  Object.assign(target, next)
}

export function reduceStreamEvent(state: StreamUiState, event: StreamEvent): ReduceResult {
  const key = `${event.stream_id}:${event.event_id}`
  if (state.seenEventKeys.has(key)) return { applied: false, gapAfter: null }

  state.seenEventKeys.add(key)
  if (state.lastSeq === 0 && event.seq > 1) state.lastSeq = event.seq - 1
  if (event.seq <= state.lastSeq) return { applied: false, gapAfter: null }

  state.pendingBySeq.set(event.seq, event)
  const gapAfter = event.seq > state.lastSeq + 1 ? state.lastSeq : null
  let applied = false

  while (state.pendingBySeq.has(state.lastSeq + 1)) {
    const next = state.pendingBySeq.get(state.lastSeq + 1)!
    state.pendingBySeq.delete(next.seq)
    applyEvent(state, next)
    state.lastSeq = next.seq
    applied = true
  }

  return { applied, gapAfter }
}

function applyEvent(state: StreamUiState, event: StreamEvent): void {
  state.streamId = event.stream_id
  state.turnId = event.turn_id
  state.traceId = event.trace_id

  switch (event.type) {
    case 'stage':
      applyStage(state, event.data)
      break
    case 'task':
      // task 事件双写：既进 activities（调试面板/明细），也回填执行计划列表对应行的状态。
      upsertActivity(state, 'task', event.data, event.event_id)
      patchPlanStatus(state, event.data)
      break
    case 'tool':
      upsertActivity(state, 'tool', event.data, event.event_id)
      break
    case 'token':
      state.response += textFrom(event.data)
      break
    case 'retract':
      // 模型重写：think→token→think→token 模式中，后端发 retract 通知清空草稿，
      // 后续 token 从头累积（最终版）。think 事件不受影响，照常展示推理过程。
      state.response = ''
      break
    case 'think':
      state.thinking += textFrom(event.data)
      break
    case 'gen_token':
      // 建站正文逐块输出（独立于聊天 token，避免污染回复气泡）。
      state.siteToken += textFrom(event.data)
      break
    case 'gen_think':
      // 建站推理过程（独立于聊天 think）。
      state.siteThink += textFrom(event.data)
      break
    case 'state_diff':
      applyStateDiff(state, event.data)
      break
    case 'approval':
      // 合并而非覆盖: 后续 approval 事件(如第一→第二步的 pending_second)往往不再携带
      // 一次性 decision_nonce 明文(后端只在首个 approval 事件下发一次), 必须保留以完成双段确认。
      state.approval = mergeApproval(state.approval, event.data)
      break
    case 'attempt_output': {
      const output = textFrom(event.data)
      if (output) state.attemptOutputs.push(output)
      break
    }
    case 'suspended':
      state.suspended = event.data
      break
    case 'usage':
      state.usage = event.data
      break
    case 'capability_notice':
      upsertCapabilityNotice(state, event.data, event.event_id)
      break
    case 'error':
      state.error = event.data
      break
    case 'reconnect':
      state.reconnect = event.data
      break
    case 'done': {
      // 终态事件可能携带最终回复文本(reply)。纯聊天/建站完成的回复只在 done
      // 里下发(不走 token 增量流), 必须把 reply 折进 state.response, 否则助手气泡
      // 永远收不到最终回答(用户侧表现为"没收到结果返回")。
      const reply = textFrom(event.data)
      if (reply) state.response = reply
      state.done = event.data
      // 本轮真实意图与执行计划的终态对账：断线重连/回放只拿到 done 时也能还原完整列表。
      applyIntents(state, event.data)
      applyPlan(state, event.data)
      break
    }
  }
}

function applyStage(state: StreamUiState, data: Record<string, unknown>): void {
  const rawId = typeof data.stage === 'string' ? data.stage : ''
  if (!STAGES.includes(rawId as StageId)) return

  const id = rawId as StageId
  const rawStatus = typeof data.status === 'string' ? data.status : ''
  const status: UiStageStatus = rawStatus === 'enter' || rawStatus === 'running'
    ? 'active'
    : rawStatus === 'leave' || rawStatus === 'completed' || rawStatus === 'skipped' || rawStatus === 'no_op'
      ? 'completed'
      : rawStatus === 'paused'
        ? 'paused'
        : rawStatus === 'blocked'
          ? 'blocked'
          : rawStatus === 'failed'
            ? 'failed'
            : state.stages[id].status

  for (const stage of STAGES) {
    if (stage < id && state.stages[stage].status === 'pending') state.stages[stage].status = 'completed'
  }

  state.stages[id] = {
    ...state.stages[id],
    status,
    label: stringValue(data.label) || state.stages[id].label,
    detail: stringValue(data.detail) || stringValue(data.reason_code) || state.stages[id].detail,
  }

  // S2 携带识别到的意图列表、S4 携带执行计划列表：在阶段出栈的那一刻就落到 state，
  // 让「理解意图」token 框收流后能立刻切换成中文意图列表，而不必等整轮 done。
  applyIntents(state, data)
  applyPlan(state, data)
}

/** 解析 intents（S2 stage 事件与 done 事件共用同一份 payload 结构）。 */
function applyIntents(state: StreamUiState, data: Record<string, unknown>): void {
  const raw = data?.intents
  if (!Array.isArray(raw)) return
  state.intents = raw
    .filter((it): it is Record<string, unknown> => Boolean(it) && typeof it === 'object')
    .map((it) => ({
      domain: String(it.domain ?? ''),
      intent_id: String(it.intent_id ?? ''),
      speechAct: String(it.speech_act ?? ''),
      // label 由后端统一给（intent_labels.py）；老版本后端没有该字段时退回 intent_id，
      // 保证不会渲染出空行。
      label: stringValue(it.label) || String(it.intent_id ?? ''),
      executable: Boolean(it.executable),
    }))
}

const PLAN_STATUSES = new Set<PlanItem['status']>(['pending', 'running', 'succeeded', 'failed', 'blocked'])

/** 解析 plan（S4 stage 事件下发骨架，done 事件回填终态）。 */
function applyPlan(state: StreamUiState, data: Record<string, unknown>): void {
  const raw = data?.plan
  if (!Array.isArray(raw)) return
  const prev = new Map(state.plan.map((item) => [item.id, item]))
  state.plan = raw
    .filter((it): it is Record<string, unknown> => Boolean(it) && typeof it === 'object')
    .map((it) => {
      const id = String(it.id ?? '')
      const incoming = stringValue(it.status) as PlanItem['status']
      const before = prev.get(id)
      // 乱序保护：S4 骨架里的 pending 不得把 task 事件已推进的 running/succeeded 打回去。
      const status: PlanItem['status'] = PLAN_STATUSES.has(incoming)
        ? (incoming === 'pending' && before && before.status !== 'pending' ? before.status : incoming)
        : (before?.status ?? 'pending')
      return {
        id,
        domain: String(it.domain ?? ''),
        intentId: String(it.intent_id ?? ''),
        label: stringValue(it.label) || String(it.intent_id ?? ''),
        status,
        durationMs: before?.durationMs,
      }
    })
}

function upsertActivity(
  state: StreamUiState,
  kind: ActivityItem['kind'],
  data: Record<string, unknown>,
  fallbackId: string,
): void {
  const id = stringValue(data.task_id) || stringValue(data.tool_call_id) || stringValue(data.id) || fallbackId
  const existing = state.activities.find((item) => item.id === `${kind}:${id}`)
  const next: ActivityItem = {
    id: `${kind}:${id}`,
    kind,
    label: stringValue(data.label) || stringValue(data.name) || stringValue(data.action) || (kind === 'task' ? '执行任务' : '调用工具'),
    status: stringValue(data.status) || 'running',
    detail: stringValue(data.detail) || stringValue(data.summary) || stringValue(data.message),
    input: data.input ?? data.args,
    output: data.output ?? data.result,
  }
  if (existing) Object.assign(existing, next)
  else state.activities.push(next)
}

/**
 * 用 S6 的 task 事件回填执行计划行状态。
 *
 * task_id 与 S4 计划条目 id 同源（ActionItem.id / 纯聊天固定 "chat"）。
 * 若计划列表尚未到达（极端乱序），就地补一行，保证状态不丢。
 */
function patchPlanStatus(state: StreamUiState, data: Record<string, unknown>): void {
  const id = stringValue(data.task_id) || stringValue(data.id)
  if (!id) return
  const incoming = stringValue(data.status) as PlanItem['status']
  if (!PLAN_STATUSES.has(incoming)) return
  const durationMs = numberValue(data.duration_ms) ?? undefined
  const row = state.plan.find((item) => item.id === id)
  if (row) {
    row.status = incoming
    if (durationMs !== undefined) row.durationMs = durationMs
    if (!row.label) row.label = stringValue(data.label)
    return
  }
  state.plan.push({
    id,
    domain: '',
    intentId: '',
    label: stringValue(data.label) || id,
    status: incoming,
    durationMs,
  })
}

function applyStateDiff(state: StreamUiState, data: Record<string, unknown>): void {
  const version = numberValue(data.version)
  if (version !== null && version <= state.stateVersion) return
  const patch = recordValue(data.patch) || recordValue(data.state) || recordValue(data.diff)
  if (!patch) return
  Object.assign(state.state, patch)
  if (version !== null) state.stateVersion = version
}

function upsertCapabilityNotice(state: StreamUiState, data: Record<string, unknown>, fallbackId: string): void {
  const id = stringValue(data.id) || stringValue(data.feature) || fallbackId
  const notice: CapabilityNotice = {
    id,
    feature: stringValue(data.feature) || '功能限制',
    tier: stringValue(data.tier) || 'L1',
    limitation: stringValue(data.limitation) || stringValue(data.message),
    upgradeHint: stringValue(data.upgrade_hint),
  }
  const existing = state.capabilityNotices.find((item) => item.id === id)
  if (existing) Object.assign(existing, notice)
  else state.capabilityNotices.push(notice)
}

function textFrom(data: Record<string, unknown>): string {
  // done 事件下发的正文在 reply 字段（后端 turns.py 构造 payload={"status","reply","artifact_refs"}），
  // 之前的取值顺序里没有 reply，导致终态回复永远取不到、助手气泡空白。reply 优先于增量字段，
  // 因为它是后端裁剪/聚合后的最终文本。
  return stringValue(data.reply) || stringValue(data.delta) || stringValue(data.text) || stringValue(data.content) || stringValue(data.message)
}

// 合并审批卡: 保留一次性 nonce(明文只下发一次), 后续事件只更新状态/风险等字段。
// 同时按状态权威度收敛: approved/rejected/expired/invalidated 等终态覆盖 pending_*。
const APPROVAL_TERMINAL = new Set(['approved', 'rejected', 'expired', 'invalidated', 'consumed', 'submitted'])
function mergeApproval(
  prev: Record<string, unknown> | null,
  next: Record<string, unknown>,
): Record<string, unknown> {
  const base = prev ?? {}
  const merged: Record<string, unknown> = { ...base, ...next }
  for (const nonceKey of ['decision_nonce', 'decision_nonce_2']) {
    if (!(nonceKey in next) && nonceKey in base) merged[nonceKey] = base[nonceKey]
  }
  const prevStatus = stringValue(base.status)
  const nextStatus = stringValue(next.status)
  // 乱序保护: 若新事件是非终态但已有终态, 保留终态(避免回退到 pending)。
  if (!APPROVAL_TERMINAL.has(nextStatus) && APPROVAL_TERMINAL.has(prevStatus)) {
    merged.status = prevStatus
  }
  return merged
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
