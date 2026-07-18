import type { ChatMessage, ModelInfo, NodeEvent, ThinkEvent } from '../types'
import { notifyAuthRequired } from '../stores/auth'

export interface ChatCallbacks {
  onNode?: (data: NodeEvent) => void
  onThink?: (data: ThinkEvent) => void
  onToken?: (text: string) => void
  onPreview?: (data: NodeEvent) => void
  onDegraded?: (data: unknown) => void
  onDone?: () => void
  onAborted?: () => void
  onError?: (msg: string) => void
}

export interface StartChatOptions {
  model: string
  messages: ChatMessage[]
  traceId: string
  conversationId: number
  cb: ChatCallbacks
}

/** 打开与业务服务的 SSE 对话流(需登录 Cookie + conversation_id)。返回 EventSource 以便取消。 */
export function startChat(opts: StartChatOptions): EventSource {
  const params = new URLSearchParams()
  params.set('model', opts.model)
  params.set('messages', JSON.stringify(opts.messages))
  params.set('trace_id', opts.traceId)
  params.set('conversation_id', String(opts.conversationId))

  const es = new EventSource(`/api/chat?${params.toString()}`)
  const safeParse = (raw: string): any => {
    try {
      return JSON.parse(raw)
    } catch {
      return {}
    }
  }

  es.addEventListener('node', (e) => opts.cb.onNode?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('think', (e) => opts.cb.onThink?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('token', (e) => {
    const d = safeParse((e as MessageEvent).data)
    const text = typeof d.data === 'string' ? d.data : (e as MessageEvent).data
    opts.cb.onToken?.(text)
  })
  es.addEventListener('preview', (e) => opts.cb.onPreview?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('degraded', (e) => opts.cb.onDegraded?.(safeParse((e as MessageEvent).data)))
  es.addEventListener('done', () => {
    opts.cb.onDone?.()
    es.close()
  })
  es.addEventListener('aborted', () => {
    opts.cb.onAborted?.()
    es.close()
  })
  // 服务端命名 error 事件带 data;网络层错误无 data。两者都关闭,不重连(避免重复生成)。
  es.addEventListener('error', (e) => {
    const me = e as MessageEvent
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
    await fetch('/api/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trace_id: traceId }),
    })
  } catch {
    /* 忽略取消失败 */
  }
}

/** 拉取可用模型列表(供选择器)。 */
export async function fetchModels(): Promise<ModelInfo[]> {
  try {
    const r = await fetch('/api/models')
    if (!r.ok) return []
    const data = await r.json()
    return Array.isArray(data) ? data : []
  } catch {
    return []
  }
}

/** 提交评价(后端若未实现 /api/feedback,静默忽略)。 */
export async function sendFeedback(
  traceId: string,
  rating: 'up' | 'down',
  comment?: string,
): Promise<void> {
  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trace_id: traceId, rating, comment }),
    })
  } catch {
    /* 静默 */
  }
}
