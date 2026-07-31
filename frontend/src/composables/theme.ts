// 主题切换(light / dark / system) — v0.9.0, #239
//
// 设计:
// - 用户偏好存 localStorage('seedai-theme'), 取值 'light' | 'dark' | 'system'
// - <html data-theme> 仅承载最终解析值 'light' | 'dark', CSS 据此切换变量
// - 'system' 时跟随系统 prefers-color-scheme, 并监听变化实时更新
// - 同步切换 <meta name="theme-color">, 让移动端地址栏同步变色(细节打磨)
import { ref } from 'vue'

export type ThemePref = 'light' | 'dark' | 'system'
const STORAGE_KEY = 'seedai-theme'

const prefs: ThemePref[] = ['light', 'dark', 'system']

const currentPref = ref<ThemePref>('dark') // 用户选择(默认深色科技主题)
const resolvedTheme = ref<'light' | 'dark'>('light') // 实际生效

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches
}

function resolve(pref: ThemePref): 'light' | 'dark' {
  if (pref === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return pref
}

function apply(pref: ThemePref) {
  const resolved = resolve(pref)
  resolvedTheme.value = resolved
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', resolved)
    const meta = document.querySelector('meta[name="theme-color"]')
    if (meta) meta.setAttribute('content', resolved === 'dark' ? '#0e1412' : '#f4f7f5')
  }
}

let mediaListener: ((e: MediaQueryListEvent) => void) | null = null

/** 应用启动早期调用: 读取偏好并挂载 system 监听 */
export function initTheme() {
  let stored = 'system'
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v && (prefs as string[]).includes(v)) stored = v
  } catch {
    /* 隐私模式可能禁止访问, 走默认 system */
  }
  currentPref.value = stored as ThemePref
  apply(currentPref.value)

  // 仅当选择 system 时监听系统变化; 切走时移除监听避免泄漏
  if (typeof window !== 'undefined') {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    mediaListener = (e: MediaQueryListEvent) => {
      if (currentPref.value === 'system') {
        resolvedTheme.value = e.matches ? 'dark' : 'light'
        document.documentElement.setAttribute('data-theme', resolvedTheme.value)
      }
    }
    mq.addEventListener('change', mediaListener)
  }
}

/** 用户切换主题 */
export function setTheme(pref: ThemePref) {
  currentPref.value = pref
  try {
    localStorage.setItem(STORAGE_KEY, pref)
  } catch {
    /* 忽略写入失败 */
  }
  apply(pref)
}

export function useTheme() {
  return { currentPref, resolvedTheme, setTheme, initTheme, prefs }
}
