<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import TopNav from './components/TopNav.vue'
import Sidebar from './components/Sidebar.vue'
import { useAuthStore } from './stores/auth'
import { useProjectStore } from './stores/project'
import { initPerfTracking } from './composables/usePerf'

// 启动前端性能追踪(首次加载时上报 TTFB/dom_ready/page_load)
initPerfTracking()

const collapsed = ref(false)
const auth = useAuthStore()
const projectStore = useProjectStore()

// 全局冷启动:先恢复登录态,登录后才拉项目;未登录不调鉴权接口。
onMounted(async () => {
  await auth.init()
  if (auth.user) {
    await projectStore.load()
  }
})

// 登录成功后自动加载项目(401 未触发 watch,仅登录成功时执行)
watch(
  () => auth.user,
  (u) => {
    if (u) projectStore.load()
  },
)
</script>

<template>
  <div class="app">
    <TopNav />
    <div class="layout">
      <Sidebar :collapsed="collapsed" @toggle="collapsed = !collapsed" />
      <main class="main"><RouterView /></main>
    </div>
  </div>
</template>

<style scoped>
.app {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.layout {
  flex: 1;
  display: flex;
  min-height: 0;
}
.main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}
</style>
