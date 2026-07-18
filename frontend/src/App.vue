<script setup lang="ts">
import { ref } from 'vue'
import TopNav from './components/TopNav.vue'
import Sidebar from './components/Sidebar.vue'

const collapsed = ref(false)
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
