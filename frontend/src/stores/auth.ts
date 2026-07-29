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
    // 软登出: 不再整页刷新(2026-07-29)。
    // 整页刷新会触发「导航抢在清 Cookie 的 Set-Cookie 落盘前」的竞态,
    // 部分浏览器下清 Cookie 被丢弃,刷新后 /auth/me 仍带旧 Cookie → 退出无效(又自动登录)。
    // 改为直接清空客户端鉴权态并弹登录遮罩: 无论后端 Cookie 是否真正清掉,
    // 页面都不可能自动登录; 全屏 .auth-mask 遮罩覆盖旧账号页面,达到同样效果。
    // SSE 关闭与业务 store 重置由 ChatView 监听 auth.user=null 处理。
    authApi.logout().catch(() => {}) // 尽力让后端清 Cookie,不阻塞前端清理
    user.value = null
    loginOpen.value = true
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
