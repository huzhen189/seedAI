/** WebLLM 引擎: 模型加载 / 状态跟踪 / 降级控制。
 *
 * 模型: Llama-3.2-1B-Instruct-q4f16  (~600MB)
 * 状态: idle → downloading → ready   (失败/不支持 → error)
 * 降级: downloading/error/unsupported 时自动回退服务端
 */

import { CreateMLCEngine, MLCEngine } from '@mlc-ai/web-llm'

/** 引擎状态 */
export type EngineStatus = 'idle' | 'downloading' | 'ready' | 'error'

/** 下载进度回调 */
export type ProgressCallback = (pct: number, text: string) => void

const MODEL = 'Llama-3.2-1B-Instruct-q4f16_1-1k' as const

let _engine: MLCEngine | null = null
let _status: EngineStatus = 'idle'
let _initPromise: Promise<MLCEngine | null> | null = null

/** 当前状态 */
export function engineStatus(): EngineStatus {
  return _status
}

/** 初始化引擎(幂等, 多次调用只加载一次) */
export async function initEngine(onProgress?: ProgressCallback): Promise<MLCEngine | null> {
  if (_engine) return _engine
  if (_initPromise) return _initPromise

  _initPromise = (async () => {
    try {
      // 检查 WebGPU 支持
      if (!(navigator as any).gpu) {
        console.warn('[WebLLM] WebGPU 不支持, 降级服务端')
        _status = 'error'
        return null
      }
      _status = 'downloading'
      console.log('[WebLLM] 引擎启动 → 开始下载模型 Llama-3.2-1B (~600MB)')
      onProgress?.(0, '正在下载模型(首次约600MB)...')
      _engine = await CreateMLCEngine(MODEL, {
        initProgressCallback: (info) => {
            const pct = Math.round((info.progress ?? 0) * 100)
            if (pct > 0 && pct % 10 === 0) console.log(`[WebLLM] 模型下载 ${pct}%`)
            onProgress?.(pct, info.text)
          },
        },
      )
      _status = 'ready'
      console.log('[WebLLM] 模型就绪 ✓')
      onProgress?.(100, '模型就绪')
      return _engine
    } catch (e) {
      console.error('[WebLLM] 加载失败, 降级服务端:', e)
      _status = 'error'
      _initPromise = null
      return null
    }
  })()
  return _initPromise
}

/** 获取已加载引擎(不等待) */
export function getEngine(): MLCEngine | null {
  return _engine
}

/** 推理一条消息, 返回文本 */
export async function chat(messages: Array<{ role: string; content: string }>): Promise<string> {
  const engine = await initEngine()
  if (!engine) throw new Error('WebLLM engine not available')
  const reply = await engine.chat.completions.create({
    messages: messages as any,
    max_tokens: 512,
    temperature: 0.7,
  })
  return reply.choices[0]?.message?.content ?? ''
}

/** 是否可用(模型已就绪且引擎存在) */
export function isReady(): boolean {
  return _status === 'ready' && _engine !== null
}
