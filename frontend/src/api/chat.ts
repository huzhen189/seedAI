import { notifyAuthRequired } from '../stores/auth'
import type { ModelInfo } from '../types'
import type { StreamEvent } from '../types/contracts.generated'

export interface ChatRequest {
  client_msg_id: string
  conversation_id: number
  message: string
  expected_conversation_version?: number
  /** 前端模型选择器透传：用户指定的执行模型(qwen/deepseek/hy3)；省略则后端走默认链。 */
  model?: string
}

/** 拉取后端已配置的可用模型列表（供前端模型选择器枚举）。 */
export async function getModels(): Promise<ModelInfo[]> {
  return requestJson('/api/models', { method: 'GET' }) as Promise<ModelInfo[]>
}

export type TurnControlAction = 'stop' | 'pause' | 'resume' | 'correct' | 'supplement' | 'discard'

export interface StreamSubscription {
  abort: () => void
  finished: Promise<void>
}

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void
  onError: (error: Error) => void
}

export interface TurnSnapshot {
  turn_id: string
  stream_id?: string
  status?: string
  response?: string
  [key: string]: unknown
}

export function startChat(request: ChatRequest, handlers: StreamHandlers): StreamSubscription {
  return openStream('/api/chat', {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  }, handlers)
}

export function replayStream(streamId: string, after: number | null, handlers: StreamHandlers): StreamSubscription {
  const params = new URLSearchParams()
  if (after != null) params.set('after', String(after))
  const query = params.size ? `?${params.toString()}` : ''
  return openStream(`/api/streams/${encodeURIComponent(streamId)}${query}`, {
    method: 'GET',
    headers: { Accept: 'text/event-stream' },
  }, handlers)
}

export async function controlTurn(
  turnId: string,
  action: TurnControlAction,
  payload: Record<string, unknown> = {},
): Promise<{ turn_id?: string; stream_id?: string; prior_turn_id?: string; status?: string } | null> {
  return (await requestJson(`/api/turns/${encodeURIComponent(turnId)}/control`, {
    method: 'POST',
    body: JSON.stringify({ action, ...payload }),
  })) as { turn_id?: string; stream_id?: string; prior_turn_id?: string; status?: string } | null
}

/**
 * 回溯修改/补充上一轮：correct=重写上一句，supplement=在上一轮基础上补充。
 * 成功返回后端新建的回溯 turn，前端据此订阅其 SSE 流拿到新版回复/产物。
 */
export async function correctTurn(
  priorTurnId: string,
  kind: 'correct' | 'supplement',
  instruction: string,
  clientMsgId: string,
): Promise<{ turn_id?: string; stream_id?: string; prior_turn_id?: string; status?: string } | null> {
  return controlTurn(priorTurnId, kind, { instruction, client_msg_id: clientMsgId })
}

export async function submitApproval(
  approvalId: string,
  decision: 'approve' | 'reject',
  decisionNonce: string,
): Promise<void> {
  await requestJson(`/api/gate/${encodeURIComponent(approvalId)}`, {
    method: 'POST',
    body: JSON.stringify({ decision, decision_nonce: decisionNonce }),
  })
}

export async function getTurn(turnId: string): Promise<TurnSnapshot> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}`, { method: 'GET' }) as Promise<TurnSnapshot>
}

export async function getPendingApprovals(): Promise<Record<string, unknown>[] | { approvals?: Record<string, unknown>[] }> {
  return requestJson('/api/gate/pending', { method: 'GET' }) as Promise<Record<string, unknown>[] | { approvals?: Record<string, unknown>[] }>
}

/** 提交用户对单轮对话的评价(后端 POST /api/feedback)。 */
export async function submitFeedback(params: {
  traceId: string
  rating: number
  comment?: string
  dimensions?: Record<string, number>
  conversationId?: number | null
}): Promise<{ ok: boolean; feedback_id: number; trace_id: string }> {
  return requestJson('/api/feedback', {
    method: 'POST',
    body: JSON.stringify({
      trace_id: params.traceId,
      rating: params.rating,
      comment: params.comment ?? null,
      dimensions: params.dimensions ?? null,
      conversation_id: params.conversationId ?? null,
    }),
  }) as Promise<{ ok: boolean; feedback_id: number; trace_id: string }>
}

function openStream(path: string, init: RequestInit, handlers: StreamHandlers): StreamSubscription {
  const controller = new AbortController()
  const finished = consume(path, { ...init, signal: controller.signal }, handlers, controller.signal)
  return { abort: () => controller.abort(), finished }
}

async function consume(
  path: string,
  init: RequestInit,
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  try {
    const response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
    })
    if (!response.ok) throw await responseError(response)
    if (!response.body) throw new Error('服务端没有返回流式响应')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      buffer = drainFrames(buffer, handlers)
    }

    buffer += decoder.decode()
    if (buffer.trim()) parseFrame(buffer, handlers)
  } catch (error) {
    if (signal.aborted) return
    handlers.onError(error instanceof Error ? error : new Error('流式连接失败'))
  }
}

function drainFrames(source: string, handlers: StreamHandlers): string {
  let rest = source
  while (true) {
    const match = /\r?\n\r?\n/.exec(rest)
    if (!match || match.index === undefined) return rest
    const frame = rest.slice(0, match.index)
    rest = rest.slice(match.index + match[0].length)
    parseFrame(frame, handlers)
  }
}

function parseFrame(frame: string, handlers: StreamHandlers): void {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n')
  if (!data) return

  try {
    const event = JSON.parse(data) as StreamEvent
    if (!isStreamEvent(event)) throw new Error('收到不符合规范的流事件')
    handlers.onEvent(event)
  } catch (error) {
    handlers.onError(error instanceof Error ? error : new Error('无法解析流事件'))
  }
}

function isStreamEvent(value: unknown): value is StreamEvent {
  if (!value || typeof value !== 'object') return false
  const event = value as Partial<StreamEvent>
  return typeof event.stream_id === 'string'
    && typeof event.turn_id === 'string'
    && typeof event.trace_id === 'string'
    && typeof event.event_id === 'string'
    && typeof event.seq === 'number'
    && typeof event.type === 'string'
    && !!event.data
    && typeof event.data === 'object'
}

async function requestJson(path: string, init: RequestInit): Promise<unknown> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...init.headers,
    },
  })
  if (!response.ok) throw await responseError(response)
  if (response.status === 204) return null
  return response.json()
}

async function responseError(response: Response): Promise<Error> {
  let message = `请求失败（${response.status}）`
  try {
    const payload = await response.json() as { message?: string; detail?: string; code?: string }
    message = payload.message || payload.detail || payload.code || message
  } catch {
    // 非 JSON 错误使用状态码即可。
  }
  if (response.status === 401) {
    notifyAuthRequired()
    message = '登录已失效，请重新登录'
  }
  return new Error(message)
}
