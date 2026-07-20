<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import TopNav from './components/TopNav.vue'
import Sidebar from './components/Sidebar.vue'
import { useAuthStore } from './stores/auth'
import { useProjectStore } from './stores/project'
import { initPerfTracking } from './composables/usePerf'

initPerfTracking()

const route = useRoute()
const collapsed = ref(false)
const auth = useAuthStore()
const projectStore = useProjectStore()

// 仅在对话页(/)显示左侧项目栏, 管理页/设置页等不显示
const showSidebar = computed(() => route.path === '/')

onMounted(async () => {
  await auth.init()
  if (auth.user) {
    await projectStore.load()
  }
})

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
      <Sidebar v-if="showSidebar" :collapsed="collapsed" @toggle="collapsed = !collapsed" />
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
