import { ref } from 'vue'
import { fetchMe, login, register, logout, type AuthUser } from '../api/auth'

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
  async function doRegister(username: string, password: string, email?: string) {
    user.value = await register(username, password, email)
  }
  async function doLogout() {
    await logout()
    user.value = null
  }
  return { user, ready, init, doLogin, doRegister, doLogout }
}
