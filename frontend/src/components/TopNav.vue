<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import AuthPanel from './AuthPanel.vue'

const router = useRouter()
const auth = useAuthStore()
const projectStore = useProjectStore()
const convStore = useConversationStore()

const searchText = ref('')

const user = computed(() => auth.user)
const searchResults = computed(() => projectStore.searchResults)

let timer: any
function onSearch() {
  clearTimeout(timer)
  timer = setTimeout(() => projectStore.search(searchText.value), 250)
}
function pickItem(item: any) {
  searchText.value = ''
  projectStore.searchResults = []
  if (item.type === 'project') {
    projectStore.currentProjectId = item.id
    router.push('/')
  } else {
    if (item.project_id != null) projectStore.currentProjectId = item.project_id
    convStore.pendingConvId = item.id
    router.push('/')
  }
}
function logout() {
  auth.logout()
}
// 点击头像进入设置页(不再用单独的"设置"按钮)
function goSettings() {
  router.push('/settings')
}
</script>

<template>
  <header class="topnav">
    <div class="brand">🌱 SeedAI</div>
    <nav class="nav">
      <RouterLink to="/" class="navlink">对话</RouterLink>
      <RouterLink to="/projects" class="navlink">项目</RouterLink>
    </nav>
    <div class="search">
      <input v-model="searchText" placeholder="搜索项目 / 会话" @input="onSearch" />
      <div v-if="searchResults.length" class="dropdown">
        <div v-for="r in searchResults" :key="r.type + r.id" class="item" @click="pickItem(r)">
          <span class="tag">{{ r.type === 'project' ? '项目' : '会话' }}</span> {{ r.title }}
        </div>
      </div>
    </div>
    <div class="right">
      <template v-if="user">
        <span class="avatar" title="点击进入设置" @click="goSettings">{{
          (user.nickname || user.username).slice(0, 1)
        }}</span>
        <span class="uname">{{ user.nickname || user.username }}</span>
        <button class="btn" @click="logout">退出</button>
      </template>
      <button v-else class="login" @click="auth.openLogin()">登录 / 注册</button>
    </div>
  </header>

  <AuthPanel />
</template>

<style scoped>
.topnav {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 10px 18px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.brand {
  font-weight: 800;
  font-size: 16px;
  color: var(--brand);
}
.nav {
  display: flex;
  gap: 6px;
}
.navlink {
  text-decoration: none;
  color: var(--muted);
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 14px;
}
.navlink.router-link-active {
  color: var(--brand);
  background: #eef2ff;
  font-weight: 600;
}
.search {
  position: relative;
  flex: 1;
  max-width: 360px;
}
.search input {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 6px 14px;
  font-size: 13px;
}
.dropdown {
  position: absolute;
  top: 38px;
  left: 0;
  right: 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
  z-index: 50;
  max-height: 300px;
  overflow: auto;
}
.item {
  padding: 8px 12px;
  font-size: 13px;
  cursor: pointer;
}
.item:hover {
  background: #f3f4f6;
}
.tag {
  font-size: 11px;
  color: #fff;
  background: var(--brand);
  border-radius: 4px;
  padding: 1px 6px;
  margin-right: 6px;
}
.right {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}
.avatar {
  cursor: pointer;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--brand);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
}
.uname {
  font-size: 13px;
  color: var(--brand);
  font-weight: 600;
}
.btn {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 8px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
  color: var(--muted);
}
.login {
  border: 1px solid var(--brand);
  background: var(--brand);
  color: #fff;
  border-radius: 8px;
  padding: 5px 14px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
</style>
