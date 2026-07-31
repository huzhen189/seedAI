import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'
import { initTracking } from './composables/track'
import { initPerfTracking } from './composables/usePerf'
import { initTheme } from './composables/theme'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// 主题初始化须尽早执行, 避免首屏白闪(在 mount 前解析 light/dark/system)
initTheme()
// 前端埋点 + 性能上报(STAT-3 / 前端性能)
initTracking()
initPerfTracking()
app.mount('#app')

// —— 本地动效库初始化(AOS 滚动揭示 / GSAP 入场) ——
app.runWithContext(() => {
  const w = window as any
  if (w.AOS) {
    w.AOS.init({ duration: 520, easing: 'ease-out-cubic', once: true, offset: 40, disable: () => false })
  }
  if (w.gsap) {
    requestAnimationFrame(() => {
      w.gsap.from('.topnav', { y: -24, opacity: 0, duration: 0.5, ease: 'power2.out' })
      w.gsap.from('.sidebar', { x: -24, opacity: 0, duration: 0.5, ease: 'power2.out', delay: 0.05 })
      w.gsap.from('.tpl-card', { y: 18, opacity: 0, duration: 0.45, stagger: 0.06, ease: 'power2.out' })
    })
  }
})
