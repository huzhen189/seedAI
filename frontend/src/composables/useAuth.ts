// 兼容封装:原有组件(AuthPanel/SettingsPanel/ChatView)通过 useAuth() 取得登录态。
// 现底层改为 Pinia useAuthStore,对外 API 保持不变,并补齐 doLogin/doRegister/doUpdateUser 别名。
import { useAuthStore } from '../stores/auth'
import { storeToRefs } from 'pinia'
import * as authApi from '../api/auth'

export function useAuth() {
  const store = useAuthStore()
  const { user, loginOpen } = storeToRefs(store)

  async function doUpdateUser(p: {
    nickname?: string
    email?: string
    oldPassword?: string
    newPassword?: string
  }) {
    const u = await authApi.updateMe(p)
    store.user = u
    return u
  }

  return {
    user,
    loginOpen,
    init: store.init,
    login: store.login,
    register: store.register,
    logout: store.logout,
    openLogin: store.openLogin,
    closeLogin: store.closeLogin,
    doLogin: store.login,
    doRegister: store.register,
    doLogout: store.logout,
    doUpdateUser,
  }
}
