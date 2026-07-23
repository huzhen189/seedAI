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
