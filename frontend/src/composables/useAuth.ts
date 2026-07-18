import { ref } from 'vue'
import { fetchMe, login, register, logout, updateMe, type AuthUser, type UpdateMePayload } from '../api/auth'

// 模块级单例:跨组件共享同一份登录态(项目未引入 Pinia)。
const user = ref<AuthUser | null>(null)
const ready = ref(false)

export function useAuth() {
  async function init() {
    user.value = await fetchMe()
    ready.value = true
  }
  async function doLogin(username: string, password: string) {
    user.value = await login(username, password)
  }
  async function doRegister(username: string, password: string, email?: string, nickname?: string) {
    user.value = await register(username, password, email, nickname)
  }
  async function doUpdateUser(p: UpdateMePayload) {
    user.value = await updateMe(p)
    return user.value
  }
  async function doLogout() {
    await logout()
    user.value = null
  }
  return { user, ready, init, doLogin, doRegister, doUpdateUser, doLogout }
}
