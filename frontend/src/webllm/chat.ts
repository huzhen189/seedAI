/** 本地对话(问候/简单问答)。仅在模型就绪 + 意图为 casual/explain 时使用。 */

import { chat, isReady } from './engine'

const CASUAL_SYSTEM = `你叫小胡，是智能建站助手。用中文像朋友一样自然回答，不要主动提建站或推销服务。回答简洁自然。`

/** 本地回复质量不够 → 需要降级服务端 */
function isLowQuality(reply: string): boolean {
  if (!reply || reply.length < 6) return true
  const refuse = ['不知道', '无法回答', '抱歉', '我没', '超出', '了解您', '请提供', '不清楚']
  return refuse.some((w) => reply.includes(w))
}

/** 本地对话(返回 null 表示不可用或质量不够，需降级服务端) */
export async function localChat(
  messages: Array<{ role: string; content: string }>,
): Promise<string | null> {
  if (!isReady()) return null
  try {
    const t0 = performance.now()
    const reply = await chat([{ role: 'system', content: CASUAL_SYSTEM }, ...messages])
    const elapsed = Math.round(performance.now() - t0)
    if (isLowQuality(reply)) {
      console.log(`[WebLLM] 本地回复质量不足(质量检测不过) ${elapsed}ms → 降级服务端`)
      return null
    }
    console.log(`[WebLLM] 本地回复: ${elapsed}ms ${reply.length}chars`)
    return reply
  } catch {
    return null
  }
}
