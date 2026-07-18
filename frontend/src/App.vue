<script setup lang="ts">
import { ref, onMounted } from 'vue'
import TopNav from './components/TopNav.vue'
import Sidebar from './components/Sidebar.vue'
import { useAuthStore } from './stores/auth'
import { useProjectStore } from './stores/project'

const collapsed = ref(false)
const auth = useAuthStore()
const projectStore = useProjectStore()

// 全局冷启动兜底:恢复登录态 + 拉取项目列表并自动选中首个项目。
// 避免某些入口视图未单独调 load 时,项目列表/选中态缺失,导致对话因 pid==null
// 被"请先新建项目"拦截(即用户反馈的"后续打开没调项目接口 / 对话不行")。
onMounted(async () => {
  await auth.init()
  await projectStore.load()
})
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
