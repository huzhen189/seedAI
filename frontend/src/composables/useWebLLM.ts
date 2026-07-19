/**
 * WebLLM 本地浏览器推理(④-b):仅本地跑 Planner, Coder 仍走云端。
 *
 * 约束(文档 §2.6):
 *   1. 锁定单固定小模型 Qwen2.5-7B-Instruct-q4f16_1-MLC(约2-5GB 权重, CDN 下载 + Cache Storage 预取)。
 *   2. 在首屏空闲时预取(warmup),减少首次调用等待。
 *   3. 本地 Planner 推理, Planner 结果 -> 云端 Coder/Reviewer。
 *   4. 本地失败自动回退云端(无感切换,前端不报错,仅标记 degraded)。
 *
 * 运行时要求:Chrome 113+ / Edge 113+ (WebGPU);Safari/Firefox 暂不支持,自动跳过。
 *
 * TODO(④-b 落地):
 *   - `npm install @mlc-ai/web-llm` 后取消下方 import 注释
 *   - 确认模型权重 CDN 可达(需网络通畅);若 CDN 不可用可换 HuggingFace mirror
 *   - 首次使用需下载权重(约 2-5GB),**请在首屏调用 warmup() 触发预取**
 *   - Chrome 需开启 WebGPU:chrome://flags/#enable-unsafe-webgpu(正式版默认已开)
 */

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
