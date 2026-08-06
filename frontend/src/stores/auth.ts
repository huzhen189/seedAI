import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as authApi from '../api/auth'
import type { AuthUser } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  // 全局登录弹窗开关:任意请求遇到 401 / "Missing authentication" 时置 true,
  // 主动弹出登录框(文档 §2.1 / 前端鉴权约定)。
  const loginOpen = ref(false)
  // 用户级偏好执行模型(user_id 绑定)。独立于 user 对象, 便于选择器即时写入而不改整份 user。
  const preferredModel = ref<string>('qwen')
  // init 幂等锁:首屏与路由守卫都会触发, 仅真正跑一次 fetchMe, 避免重复请求。
  let initialized = false

  function syncPreferred(u: AuthUser | null) {
    if (u?.preferredModel) preferredModel.value = u.preferredModel
  }

  async function init() {
    // 已在 App.vue 提前 init 过则直接复用, 不重复发请求。
    if (initialized) return
    initialized = true
    try {
      const u = await authApi.fetchMe()
      user.value = u
      syncPreferred(u)
    } catch {
      user.value = null
    }
  }
  async function login(account: string, password: string): Promise<AuthUser> {
    const u = await authApi.login(account, password)
    user.value = u
    syncPreferred(u)
    loginOpen.value = false // 登录成功自动关闭弹窗
    return u
  }
  async function register(
    account: string,
    password: string,
    email?: string,
    displayName?: string,
  ): Promise<AuthUser> {
    const u = await authApi.register(account, password, email, displayName)
    user.value = u
    syncPreferred(u)
    loginOpen.value = false
    return u
  }
  /** 选择器切换时调用: 写入后端(携带 user_id)并同步本地回显。 */
  async function setPreferredModel(model: string) {
    preferredModel.value = model
    try {
      await authApi.setPreferredModel(model)
    } catch {
      /* 静默: 后端兜底会在下次发消息时收敛, 见后端 accept() */
    }
  }
  async function logout() {
    // 退出即整页刷新: 先等后端清 Cookie 的 Set-Cookie 真正落盘(await 响应体),
    // 再 reload —— 避免早期「reload 抢在清 Cookie 前」导致退出无效的竞态。
    // 刷新后 App 重新 init → fetchMe 返回 null → 登录态彻底清空,
    // 旧账号的项目/会话/对话缓存一并被 Vue 重新挂载清除,切换账号不会串数据。
    try {
      await authApi.logout() // 内部已 await 响应体, 确保浏览器处理完清 Cookie
    } catch {
      /* 网络失败也继续刷新, 让登录遮罩接管 */
    }
    user.value = null
    // 清理前端本地缓存(SSE 断流、会话 resume 等), 避免旧账号数据泄露到新登录态。
    try {
      sessionStorage.removeItem('seedai:stream-resume')
      sessionStorage.removeItem('seedai:preview-ratio')
    } catch { /* ignore */ }
    loginOpen.value = true
    // 关键: 等上面的清理落定后再整页刷新, 用户看到的是干干净净的登录页。
    window.location.reload()
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
    preferredModel,
    init,
    login,
    register,
    logout,
    setPreferredModel,
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
