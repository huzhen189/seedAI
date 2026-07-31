<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import { useTheme, type ThemePref } from '../composables/theme'
import AuthPanel from './AuthPanel.vue'
import Icon from './Icon.vue'

const router = useRouter()
const auth = useAuthStore()
const projectStore = useProjectStore()
const convStore = useConversationStore()
const { currentPref, setTheme } = useTheme()

const searchText = ref('')

const user = computed(() => auth.user)
const searchResults = computed(() => projectStore.searchResults)

// 主题分段控件(label + 图标)
const themeOptions: { value: ThemePref; icon: string; label: string }[] = [
  { value: 'light', icon: 'sun', label: '亮色' },
  { value: 'dark', icon: 'moon', label: '暗色' },
  { value: 'system', icon: 'monitor', label: '跟随系统' },
]

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
  <header class="topnav glass-surface">
    <div class="brand">
      <Icon name="logo" :size="20" class="brand-ico" />
      <span class="brand-text">Seed<span class="brand-gradient">AI</span></span>
    </div>
    <nav class="nav">
      <RouterLink to="/" class="navlink" data-track="导航:对话">对话</RouterLink>
      <RouterLink to="/projects" class="navlink" data-track="导航:项目">项目</RouterLink>
      <RouterLink
        v-if="user && (user.role === 'admin' || user.role === 'super_admin')"
        to="/admin"
        class="navlink"
        data-track="导航:管理"
        >管理</RouterLink
      >
    </nav>
    <div class="search">
      <Icon name="search" :size="15" style="position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none" />
      <input v-model="searchText" placeholder="搜索项目 / 会话" @input="onSearch" style="padding-left:34px" />
      <div v-if="searchResults.length" class="dropdown">
        <div v-for="r in searchResults" :key="r.type + r.id" class="item" @click="pickItem(r)">
          <span class="tag">{{ r.type === 'project' ? '项目' : '会话' }}</span> {{ r.title }}
        </div>
      </div>
    </div>
    <div class="right">
      <!-- 主题切换(light/dark/system) v0.9.0 #239 -->
      <div class="theme-switch" role="group" aria-label="主题切换">
        <button
          v-for="opt in themeOptions"
          :key="opt.value"
          class="theme-opt"
          :class="{ active: currentPref === opt.value }"
          :title="opt.label"
          :aria-pressed="currentPref === opt.value"
          @click="setTheme(opt.value)"
        >
          <Icon :name="opt.icon" :size="15" />
        </button>
      </div>
      <template v-if="user">
        <span class="avatar" title="点击进入设置" @click="goSettings">{{
          (user.nickname || user.account).slice(0, 1)
        }}</span>
        <span class="uname">{{ user.nickname || user.account }}</span>
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
  position: relative;
  z-index: 30;
}
.topnav::after {
  content: '';
  position: absolute;
  left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--brand-border), transparent);
  opacity: 0.5;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 800;
  font-size: 16px;
  cursor: pointer;
}
.brand-ico {
  color: var(--brand);
  filter: drop-shadow(0 0 8px var(--brand-bg));
}
.brand-text { letter-spacing: 0.3px; }
.brand-text .brand-gradient { font-weight: 900; }
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
  background: var(--active-bg);
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
  background: var(--panel);
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
  background: var(--hover-bg);
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
  background: var(--brand-grad);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  box-shadow: 0 4px 12px rgba(21,196,164,.3);
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
  border: 1px solid transparent;
  background: var(--brand-grad);
  color: #fff;
  border-radius: 999px;
  padding: 5px 16px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 0 6px 16px rgba(21,196,164,.28);
  transition: transform var(--transition-fast), box-shadow var(--transition-fast), filter var(--transition-fast);
}
.login:hover { transform: translateY(-2px); box-shadow: 0 10px 22px rgba(21,196,164,.36); filter: brightness(1.03); }
/* 主题切换分段控件(v0.9.0 #239) */
.theme-switch {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  background: var(--hover-bg);
  border: 1px solid var(--border);
  border-radius: 999px;
}
.theme-opt {
  border: none;
  background: transparent;
  cursor: pointer;
  width: 28px;
  height: 26px;
  border-radius: 999px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  line-height: 1;
  transition: background-color 0.2s ease, transform 0.15s ease;
}
.theme-opt:hover {
  transform: translateY(-1px);
}
.theme-opt.active {
  background: var(--panel);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
}
.theme-opt .ico {
  filter: grayscale(0.2);
}
.theme-opt.active .ico {
  filter: none;
}
</style>
