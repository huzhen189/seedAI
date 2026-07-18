import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useAuthStore, notifyAuthRequired } from './auth'

// mock 掉真实 api 层(fetch),专注测试 store 自身行为。
vi.mock('../api/auth', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}))

import * as authApi from '../api/auth'

const fakeUser = {
  id: 1,
  username: 'u',
  nickname: 'u',
  email: null,
  role: 'user',
  plan: 'free',
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('auth store', () => {
  it('openLogin / closeLogin 切换登录弹窗', () => {
    const s = useAuthStore()
    expect(s.loginOpen).toBe(false)
    s.openLogin()
    expect(s.loginOpen).toBe(true)
    s.closeLogin()
    expect(s.loginOpen).toBe(false)
  })

  it('notifyAuthRequired 打开登录弹窗(供 api 层在 401 时调用)', () => {
    notifyAuthRequired()
    expect(useAuthStore().loginOpen).toBe(true)
  })

  it('login 成功后写入用户并自动关闭弹窗(v0.2.1 关键行为)', async () => {
    vi.mocked(authApi.login).mockResolvedValue(fakeUser)
    const s = useAuthStore()
    s.openLogin()
    const u = await s.login('u', 'p')
    expect(u.username).toBe('u')
    expect(s.user).not.toBeNull()
    expect(s.loginOpen).toBe(false)
  })

  it('login 失败时弹窗保持打开', async () => {
    vi.mocked(authApi.login).mockRejectedValue(new Error('invalid'))
    const s = useAuthStore()
    s.openLogin()
    await expect(s.login('u', 'bad')).rejects.toThrow('invalid')
    expect(s.loginOpen).toBe(true)
  })
})
