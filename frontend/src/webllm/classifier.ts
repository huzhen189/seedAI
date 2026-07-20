/** 本地意图分类(WebLLM)。
 * 只在模型就绪时使用, 否则回退服务端。
 */

import { chat, isReady } from './engine'

/** 分类结果 */
export interface IntentResult {
  level1: string
  level2: string
  confidence: number
  industry: string
  /** 'local' | 'server' */
  source: string
}

const CLASSIFIER_PROMPT = `你是建站助手的意图分类器。只返回JSON: {"level1":"...","level2":"...","confidence":0.0~1.0,"industry":"..."}

level1(6选1): learn|code|build|doc|translate|unsupported
learn: explain(问答)|casual(闲聊问候)|design(UI设计)|search(搜索)
code: snippet(代码片段)|component(UI组件)|fix(修Bug)|refactor(评审)
build: page(单页)|site(完整站)|modify(修改)|game(小游戏)
doc: readme|tutorial|plan(方案)
translate: text

用户: `

/** 本地意图分类 */
export async function localClassify(text: string): Promise<IntentResult | null> {
  if (!isReady()) {
    console.log('[WebLLM] 本地分类跳过(引擎未就绪) → 降级服务端')
    return null
  }
  try {
    const t0 = performance.now()
    const raw = await chat([{ role: 'user', content: CLASSIFIER_PROMPT + text }])
    const json = JSON.parse(raw.match(/\{[\s\S]*\}/)?.[0] || raw)
    const elapsed = Math.round(performance.now() - t0)
    const result: IntentResult = {
      level1: json.level1 || 'learn',
      level2: json.level2 || 'explain',
      confidence: json.confidence ?? 0.7,
      industry: json.industry || 'none',
      source: 'local',
    }
    console.log(`[WebLLM] 本地分类: "${text.substring(0,30)}" → ${result.level1}/${result.level2} (conf=${result.confidence}) ${elapsed}ms`)
    return result
  } catch (e) {
    console.log('[WebLLM] 本地分类失败 → 降级服务端:', (e as Error).message)
    return null
  }
}
