/**
 * 统一鉴权 fetch 封装。
 *
 * 规则:
 *   1. 请求前若未登录 → 打开登录弹窗 + 抛 AUTH_REQUIRED(不发起请求)
 *   2. 响应 401 → 打开登录弹窗 + 抛 AUTH_REQUIRED(token 过期)
 *   3. 响应非 2xx → 抛 Error(后端 detail 或 HTTP status)
 *
 * 用法:
 *   import { get, post, patch, del } from './client'
 *   const data = await get('/api/projects')
 *   await post('/api/projects', { name: 'test' })
 */

import { useAuthStore } from '../stores/auth'
import { notifyAuthRequired } from '../stores/auth'

async function request(method: string, path: string, body?: unknown): Promise<any> {
  // ① 请求前鉴权门禁
  let auth: ReturnType<typeof useAuthStore> | null = null
  try {
    auth = useAuthStore()
  } catch {
    // pinia 尚未激活(极早期调用) → 放过,由调用方处理
  }
  if (auth && !auth.user) {
    auth.openLogin()
    // 请求未发送,不抛异常(静默拦截):调用方收到 undefined/跳过
    // 对于 async 调用,抛 Error 更安全;对于同步场景,静默跳过
    throw new Error('AUTH_REQUIRED')
  }

  const r = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include',
  })

  // ② 响应 401 → token 过期,弹出登录
  if (r.status === 401) {
    notifyAuthRequired()
    throw new Error('AUTH_REQUIRED')
  }

  // ③ 响应非 2xx
  if (!r.ok) {
    const text = await r.text().catch(() => '')
    throw new Error(text || `HTTP ${r.status}`)
  }

  if (r.status === 204) return null
  return r.json()
}

export const get = (path: string) => request('GET', path)
export const post = (path: string, body?: unknown) => request('POST', path, body)
export const patch = (path: string, body?: unknown) => request('PATCH', path, body)
export const del = (path: string) => request('DELETE', path)

/** 公开接口(无需登录),直接 fetch */
export async function publicGet(path: string): Promise<any> {
  const r = await fetch(path, { credentials: 'include' })
  if (!r.ok) {
    const text = await r.text().catch(() => '')
    throw new Error(text || `HTTP ${r.status}`)
  }
  return r.json()
}
