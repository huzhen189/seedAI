import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'
import { initTracking } from './composables/track'
import { initPerfTracking } from './composables/usePerf'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// 前端埋点 + 性能上报(STAT-3 / 前端性能)
initTracking()
initPerfTracking()
app.mount('#app')
