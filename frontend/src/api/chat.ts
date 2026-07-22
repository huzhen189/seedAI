import type { ChatMessage, IntentEvent, ModelInfo, NodeEvent, OptionEvent, AlternativesEvent, PlanEvent, RetryEvent, ThinkEvent, UnsupportedEvent, BlockEvent, ConfirmEvent, QcResult, RatingDims, OrchestrationEvent, SubTaskStartEvent, SubTaskDoneEvent, SubTaskFailEvent, MergeEvent } from '../types'
import { notifyAuthRequired } from '../stores/auth'
import { post, publicGet } from './client'

export interface ChatCallbacks {
  onNode?: (data: NodeEvent) => void
  onThink?: (data: ThinkEvent) => void
  onPlan?: (data: PlanEvent) => void
  onToken?: (text: string, subTaskId?: string) => void
  onPreview?: (data: NodeEvent) => void
  onDegraded?: (data: unknown) => void
  onDone?: () => void
  onAborted?: () => void
  onError?: (msg: string) => void
  /** 主模型不可用时触发(data 含 failed/suggested/message),前端弹框待用户选替代模型后重发 */
  onRetry?: (data: RetryEvent) => void
  /** 用户断开连接, 已保存断点(data 含 stage/progress) */
  onPaused?: (data: Record<string, unknown>) => void
  onRequirement?: (data: Record<string, unknown>) => void
  /** 多选项(requirement_agent 出方案) */
  onIntent?: (data: IntentEvent) => void
  /** 多方案选择(requirement_agent 出方案),前端弹出单选框 */
  onOptions?: (data: OptionEvent) => void
  /** 非阻塞候选提示(管道级工具路由已自决 top-1, 列出可切换候选) */
  onAlternatives?: (data: AlternativesEvent) => void
  /** 不支持的功能提示(意图不属于已知范围) */
  onUnsupported?: (data: UnsupportedEvent) => void
  /** 高危拦截(安全 critical, 不可绕过) */
  onBlock?: (data: BlockEvent) => void
  /** 二次确认(安全 high, 等待用户确认后带 confirmed 重发) */
  onConfirm?: (data: ConfirmEvent) => void
  /** 多意图编排总览(orchestration):子任务清单 + 执行策略 */
  onOrchestration?: (data: OrchestrationEvent) => void
  /** 子任务开始进入执行层(subtask_start) */
  onSubtaskStart?: (data: SubTaskStartEvent) => void
  /** 子任务完成(subtask_done) */
  onSubtaskDone?: (data: SubTaskDoneEvent) => void
  /** 子任务失败 / 拦截 / 跳过(subtask_fail) */
  onSubtaskFail?: (data: SubTaskFailEvent) => void
  /** 结果合并完成(merge):最终连贯回复 + 部分失败清单 */
  onMerge?: (data: MergeEvent) => void
  /** 后置 QC 三裁判结果(v0.8.5 M1):整体分 + 6 维聚合, 落入气泡徽标 */
  onQc?: (data: QcResult) => void
}

export interface StartChatOptions {
  model: string
  messages?: ChatMessage[]  // 已废弃: 后端从 DB 取历史, 前端只传 q
  traceId: string
  conversationId: number
  cb: ChatCallbacks
  q?: string
  contextHint?: string
  after?: string
  resume?: boolean
  correct?: boolean
  /** 二次确认已通过标记(安全 confirm 通过后重发) */
  confirmed?: boolean
  /** 多选项选中后重发: 指定 Worker 直接执行的 skill(管道级 options 选择) */
  skill?: string
  /** 多意图中风险已确认的子任务 id 列表(重发时带上, 让 MEDIUM 风险子任务放行) */
  confirmedSubtasks?: string[]
}

/** 打开与业务服务的 SSE 对话流(需登录 Cookie + conversation_id)。返回 EventSource 以便取消。 */
export function startChat(opts: StartChatOptions): EventSource {
  const params = new URLSearchParams()
  params.set('model', opts.model)
  params.set('trace_id', opts.traceId)
  params.set('conversation_id', String(opts.conversationId))
  if (opts.q) params.set('q', opts.q)
  if (opts.contextHint) params.set('context_hint', opts.contextHint)
  if (opts.after) params.set('after', opts.after)
  if (opts.resume) params.set('resume', 'true')
  if (opts.correct) params.set('correct', 'true')
  if (opts.confirmed) params.set('confirmed', '1')
  if (opts.skill) params.set('skill', opts.skill)
  if (opts.confirmedSubtasks?.length) params.set('confirmed_subtasks', opts.confirmedSubtasks.join(','))

  const url = `/api/chat?${params.toString()}`
  console.log('[SSE] 连接 %s', url)
  const es = new EventSource(url)
  es.onopen = () => console.log('[SSE] 已连接')
  const safeParse = (raw: string): any => {
    try {
      return JSON.parse(raw)
    } catch {
      return {}
    }
  }

  es.addEventListener('node', (e) => opts.cb.onNode?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('think', (e) => opts.cb.onThink?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('plan', (e) => opts.cb.onPlan?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('intent', (e) => opts.cb.onIntent?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('options', (e) => opts.cb.onOptions?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('alternatives', (e) => opts.cb.onAlternatives?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('unsupported', (e) => opts.cb.onUnsupported?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('block', (e) => opts.cb.onBlock?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('confirm', (e) => opts.cb.onConfirm?.(safeParse((e as MessageEvent).data)))
  // 多意图编排事件(sub_task_id 已在 proxy 透传, 前端按事件渲染泳道)
  es.addEventListener('orchestration', (e) => opts.cb.onOrchestration?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('subtask_start', (e) => opts.cb.onSubtaskStart?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('subtask_done', (e) => opts.cb.onSubtaskDone?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('subtask_fail', (e) => opts.cb.onSubtaskFail?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('merge', (e) => opts.cb.onMerge?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('paused', (e) => opts.cb.onPaused?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('requirement_doc', (e) => opts.cb.onRequirement?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('token', (e) => {
    const d = safeParse((e as MessageEvent).data)
    const text = typeof d.data === 'string' ? d.data : (e as MessageEvent).data
    // 多意图: token 带 sub_task_id(单意图为 undefined;合并结果 = "__merge__")
    const subTaskId = typeof d.sub_task_id === 'string' ? d.sub_task_id : undefined
    opts.cb.onToken?.(text, subTaskId)
  })
  es.addEventListener('preview', (e) => opts.cb.onPreview?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('degraded', (e) => opts.cb.onDegraded?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('qc', (e) => {
    const d = safeParse((e as MessageEvent).data) as QcResult
    opts.cb.onQc?.(d)
  })
  es.addEventListener('done', () => {
    console.log('[SSE] 收到 done, 关闭连接')
    opts.cb.onDone?.()
    es.close()
  })
  es.addEventListener('aborted', () => {
    console.log('[SSE] 收到 aborted')
    opts.cb.onAborted?.()
    es.close()
  })
  // 主模型不可用:前端弹框确认后重新发起请求
  es.addEventListener('retry', (e) => {
    const d = safeParse((e as MessageEvent).data) as RetryEvent
    opts.cb.onRetry?.(d)
    es.close()
  })
  // 服务端命名 error 事件带 data;网络层错误无 data。两者都关闭,不重连(避免重复生成)。
  es.addEventListener('error', (e) => {
    const me = e as MessageEvent
    console.log('[SSE] 收到 error data=%s', me.data ? String(me.data).slice(0, 100) : '(无)')
    if (me.data) {
      const d = safeParse(me.data)
      const msg = String(d.message || me.data)
      const code = String(d.code || '')
      // 按后端下发的错误码给出明确提示,而非笼统“连接中断”。
      if (code === 'UPSTREAM_ERROR') {
        opts.cb.onError?.('AI 服务暂时不可用，请稍后重试')
      } else if (code === 'RATE_LIMITED') {
        opts.cb.onError?.('请求过于频繁，请稍后再试')
      } else if (code === 'AUTH_REQUIRED') {
        // 鉴权失败:主动弹出登录框。
        notifyAuthRequired()
        opts.cb.onError?.('登录已失效，请重新登录')
      } else if (
        /missing authentication|not authenticated|未登录|invalid or expired token|^\s*401\b/i.test(
          msg,
        )
      ) {
        // 兜底:无 code 但文案为鉴权失败。
        notifyAuthRequired()
        opts.cb.onError?.('登录已失效，请重新登录')
      } else {
        // 其他服务端错误:显示后端给出的 message(默认也给出可读文案)。
        opts.cb.onError?.(msg || '服务异常，请稍后重试')
      }
    } else {
      // 无 data:多为网络中断(连接被重置/服务不可达);EventSource 对
      // 非 2xx 也会以无 data error 触发,故提示“检查网络或重新登录”。
      opts.cb.onError?.('连接中断，请检查网络或重新登录')
    }
    es.close()
  })

  return es
}

/** 级联取消(C1):通知业务 → AI 标记 cancel。 */
export async function cancelChat(traceId: string): Promise<void> {
  try {
    await post('/api/cancel', { trace_id: traceId })
  } catch {
    /* 忽略取消失败 */
  }
}

/** 拉取可用模型列表(公开接口,无需登录)。 */
export async function fetchModels(): Promise<ModelInfo[]> {
  try {
    const data = await publicGet('/api/models')
    return Array.isArray(data) ? data : []
  } catch {
    return []
  }
}

/** 提交 1-10 评分评价(③-a:统计 + 回归数据集)。后端 /api/feedback 已实现。
 *  dimensions: 气泡内 6 维细分(可选), 缺省 null。 */
export async function sendFeedback(
  traceId: string,
  rating: number,
  conversationId?: number,
  comment?: string,
  dimensions?: RatingDims,
): Promise<boolean> {
  try {
    await post('/api/feedback', {
      trace_id: traceId,
      conversation_id: conversationId ?? null,
      rating,
      comment: comment || null,
      dimensions: dimensions && Object.keys(dimensions).length ? dimensions : null,
    })
    return true
  } catch {
    return false
  }
}
