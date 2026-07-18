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
  async function login(username: string, password: string): Promise<AuthUser> {
    const u = await authApi.login(username, password)
    user.value = u
    loginOpen.value = false // 登录成功自动关闭弹窗
    return u
  }
  async function register(
    username: string,
    password: string,
    email?: string,
    nickname?: string,
  ): Promise<AuthUser> {
    const u = await authApi.register(username, password, email, nickname)
    user.value = u
    loginOpen.value = false
    return u
  }
  async function logout() {
    await authApi.logout()
    user.value = null
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
