/**
 * WebLLM 本地浏览器推理——暂时弃用(v1.0)，后续升级后重新启用。
 *
 * 弃用原因: @mlc-ai/web-llm 模型权重太大(2-5GB), 本地HTTP环境COOP/COEP不兼容,
 * 目前云端推理完全满足需求。后续升级时取消下方 DISABLED 即可恢复。
 */
export const WEBLLM_DISABLED = true

// import { CreateMLCEngine, MLCEngine } from '@mlc-ai/web-llm'

const MODEL_ID = 'Qwen2.5-7B-Instruct-q4f16_1-MLC'

// Re-export for ModelSelector / debugging
export { MODEL_ID }

let engine: any = null
let ready = false
let warming = false

/** 检查运行环境是否支持 WebLLM */
export function isWebGPUSupported(): boolean {
  return typeof navigator !== 'undefined' && 'gpu' in navigator
}

/** 预取模型权重(首屏空闲时调用,缩短首次生成等待)。幂等,重复调不重新下载。 */
export async function warmupWebLLM(): Promise<void> {
  if (warming || ready || !isWebGPUSupported()) return
  warming = true
  try {
    // const { CreateMLCEngine } = await import('@mlc-ai/web-llm')
    // engine = await CreateMLCEngine(MODEL_ID, {
    //   initProgressCallback: (p: { progress: number; text: string }) => {
    //     console.log(`[WebLLM] ${p.text} (${(p.progress * 100).toFixed(1)}%)`)
    //   },
    // })
    // ready = true
    console.log('[WebLLM] warmup skipped — @mlc-ai/web-llm 未安装或代码未取消注释')
  } catch (e) {
    console.warn('[WebLLM] warmup failed:', e)
  } finally {
    warming = false
  }
}

/** 本地 Planner 规划(④-b)。失败返回 null,由调用方回退云端。 */
export async function localPlanner(
  requirement: string,
  system?: string,
): Promise<{ spec: string } | null> {
  if (!ready || !engine) {
    if (isWebGPUSupported()) {
      // 尚未 warmup,尝试即时加载
      await warmupWebLLM()
    }
    if (!ready || !engine) return null
  }
  try {
    const prompt = `${
      system || '你负责把用户的建站需求拆解成结构化规格。请只输出一个 JSON 对象。'
    }\n\n用户需求:\n${requirement}`
    void prompt // TODO: 取消 engine.chat.completions 注释后删此行
    // const reply = await engine.chat.completions.create({
    //   messages: [{ role: 'user', content: prompt }],
    //   max_tokens: 1024,
    // })
    // const spec = reply.choices[0]?.message?.content || ''
    return null // TODO: 取消上方注释启用本地推理
  } catch (e) {
    console.warn('[WebLLM] localPlanner failed:', e)
    return null
  }
}

/** 重置引擎(切换模型/释放内存) */
export function resetWebLLM(): void {
  if (engine) {
    // engine.unload?.()
    engine = null
  }
  ready = false
  warming = false
}
