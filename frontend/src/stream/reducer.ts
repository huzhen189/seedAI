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
    case 'tool':
      upsertActivity(state, event.type, event.data, event.event_id)
      break
    case 'token':
      state.response += textFrom(event.data)
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
    case 'done':
      state.done = event.data
      break
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
  return stringValue(data.delta) || stringValue(data.text) || stringValue(data.content) || stringValue(data.message)
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
