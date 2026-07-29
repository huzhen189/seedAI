import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as authApi from '../api/auth'
import type { AuthUser } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  // 全局登录弹窗开关:任意请求遇到 401 / "Missing authentication" 时置 true,
  // 主动弹出登录框(文档 §2.1 / 前端鉴权约定)。
  const loginOpen = ref(false)

  async function init() {
    user.value = await authApi.fetchMe()
  }
  async function login(account: string, password: string): Promise<AuthUser> {
    const u = await authApi.login(account, password)
    user.value = u
    loginOpen.value = false // 登录成功自动关闭弹窗
    return u
  }
  async function register(
    account: string,
    password: string,
    email?: string,
    nickname?: string,
  ): Promise<AuthUser> {
    const u = await authApi.register(account, password, email, nickname)
    user.value = u
    loginOpen.value = false
    return u
  }
  async function logout() {
    // 后端清除 HttpOnly Cookie,然后整页刷新。
    // 刷新能保证彻底清空所有 Pinia store(project/conversation/agent)、
    // 进行中的 EventSource(SSE) 连接与消息缓存,避免切换账号后
    // 仍残留上一个账号的页面/数据(2026-07-29 用户要求)。
    // 关键: 先等登出请求完整处理(含 Set-Cookie 清 Cookie),再让出当前宏任务,
    // 确保浏览器已把清 Cookie 落到 Cookie Jar,然后才发起整页刷新;
    // 否则 location.reload() 可能在 Cookie 落盘前抢先导航,清 Cookie 被丢弃 → 退出无效。
    await authApi.logout()
    user.value = null
    await new Promise((resolve) => setTimeout(resolve, 0))
    if (typeof location !== 'undefined') {
      location.reload()
    }
  }

  function openLogin() {
    loginOpen.value = true
  }
  function closeLogin() {
    loginOpen.value = false
  }
  /** 鉴权失败统一入口:打开登录弹窗(供 api 层在 401 时调用)。 */
  function requireLogin() {
    openLogin()
  }

  return {
    user,
    loginOpen,
    init,
    login,
    register,
    logout,
    openLogin,
    closeLogin,
    requireLogin,
  }
})

/** 供非组件模块(如 api/*)在运行时调用,无需传入 store 实例。
 *  即使 pinia 未就绪也安全忽略,避免初始化阶段崩溃。 */
export function notifyAuthRequired() {
  try {
    useAuthStore().requireLogin()
  } catch {
    /* pinia 未激活时忽略 */
  }
}
