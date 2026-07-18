import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as authApi from '../api/auth'
import type { AuthUser } from '../types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)

  async function init() {
    user.value = await authApi.fetchMe()
  }
  async function login(username: string, password: string): Promise<AuthUser> {
    const u = await authApi.login(username, password)
    user.value = u
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
    return u
  }
  async function logout() {
    await authApi.logout()
    user.value = null
  }

  return { user, init, login, register, logout }
})
