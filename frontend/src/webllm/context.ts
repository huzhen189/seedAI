/** 上下文关联检测(WebLLM)。判断当前输入与最近对话是否有关。 */

import { chat, isReady } from './engine'

const CTX_SYSTEM = '你是上下文检测器。只输出一句话描述上文在聊什么，没有明显上下文则输出一个字: 无。'

/** 检测上下文(返回 null 表示不可用/无关联) */
export async function contextCheck(
  text: string,
  history: Array<{ role: string; content: string }>,
): Promise<string | null> {
  if (!isReady()) return null
  try {
    const t0 = performance.now()
    const msgs = [
      ...history.slice(-12).map(m => ({ role: m.role, content: m.content.substring(0, 200) })),
      { role: 'user', content: `用户刚说: "${text}"。上文在聊什么？一句话总结，没有则输出"无"。` },
    ]
    const reply = await chat([{ role: 'system', content: CTX_SYSTEM }, ...msgs])
    const elapsed = Math.round(performance.now() - t0)
    const trimmed = reply.trim()
    if (!trimmed || trimmed === '无' || trimmed.length < 2) {
      console.log(`[WebLLM] 上下文检测: 无关联 ${elapsed}ms`)
      return null
    }
    console.log(`[WebLLM] 上下文检测: "${trimmed.substring(0, 60)}" ${elapsed}ms`)
    return trimmed
  } catch {
    return null
  }
}
