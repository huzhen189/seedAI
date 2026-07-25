<script setup lang="ts">
import { ref, watch } from 'vue'
import { useProjectStore } from '../stores/project'
import { searchMessages, type MessageSearchResult } from '../api/projects'

defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{
  toggle: []
  'navigate-message': [result: MessageSearchResult]
}>()
const projectStore = useProjectStore()

const showSearch = ref(false)
const searchText = ref('')
const msgResults = ref<MessageSearchResult[]>([])
const searching = ref(false)

async function newProject() {
  const name = prompt('项目名称：', '我的项目')
  if (!name) return
  await projectStore.create(name)
}
function selectProject(id: number) {
  projectStore.currentProjectId = id
}

let _searchTimer: ReturnType<typeof setTimeout> | null = null
async function onSearch() {
  const q = searchText.value.trim()
  // 同时调用旧搜索(项目/会话名) + 新搜索(消息内容)
  if (_searchTimer) clearTimeout(_searchTimer)
  if (!q) {
    projectStore.searchResults = []
    msgResults.value = []
    return
  }
  _searchTimer = setTimeout(async () => {
    searching.value = true
    try {
      await projectStore.search(q)
      msgResults.value = await searchMessages(q)
    } finally {
      searching.value = false
    }
  }, 300)
}

function pickSearch(item: any) {
  searchText.value = ''
  projectStore.searchResults = []
  msgResults.value = []
  if (item.type === 'project') projectStore.currentProjectId = item.id
  else if (item.project_id != null) projectStore.currentProjectId = item.project_id
  showSearch.value = false
}

function pickMessage(r: MessageSearchResult) {
  searchText.value = ''
  projectStore.searchResults = []
  msgResults.value = []
  showSearch.value = false
  // 存储导航目标, ChatView 读取后滚动到指定消息
  sessionStorage.setItem('nav_conv', String(r.conversation_id))
  sessionStorage.setItem('nav_msg', String(r.message_id))
  // 先切项目, ChatView watch 到 projectId 变化后会读取 sessionStorage
  projectStore.currentProjectId = r.project_id
}

// 展开搜索框时聚焦
watch(showSearch, (v) => {
  if (v) {
    setTimeout(() => {
      const inp = document.querySelector('.searchbox input') as HTMLInputElement
      inp?.focus()
    }, 50)
  }
})
</script>

<template>
  <aside class="sidebar" :class="{ collapsed }">
    <div class="actions">
      <template v-if="!collapsed">
        <button class="act" title="新建项目" @click="newProject">＋</button>
        <button class="act" title="搜索" @click="showSearch = !showSearch">🔍</button>
      </template>
      <button class="act toggle-btn" :title="collapsed ? '展开' : '收起'" @click="emit('toggle')">
        {{ collapsed ? '▶' : '◀' }}
      </button>
    </div>

    <div v-if="showSearch && !collapsed" class="searchbox">
      <input v-model="searchText" placeholder="搜索消息 / 项目 / 会话" @input="onSearch" />
      <div v-if="searching" class="search-hint">搜索中…</div>
      <!-- 旧搜索: 项目/会话名 -->
      <div v-if="projectStore.searchResults.length" class="sres">
        <div class="sres-label">匹配的项目/会话</div>
        <div
          v-for="r in projectStore.searchResults"
          :key="r.type + r.id"
          class="sitem"
          @click="pickSearch(r)"
        >
          {{ r.type === 'project' ? '📁' : '💬' }} {{ r.title }}
        </div>
      </div>
      <!-- 消息搜索: 匹配的 Q&A 对 -->
      <div v-if="msgResults.length" class="sres msg-results">
        <div class="sres-label">匹配的消息 ({{ msgResults.length }})</div>
        <div
          v-for="r in msgResults"
          :key="'m' + r.message_id"
          class="sitem msg-item"
          @click="pickMessage(r)"
        >
          <div class="msg-meta">📁 {{ r.project_name }} · 💬 {{ r.conv_title }}</div>
          <div class="msg-user">🙋 {{ r.user_text }}</div>
          <div v-if="r.ai_reply" class="msg-ai">🤖 {{ r.ai_reply }}</div>
        </div>
      </div>
      <div v-if="!searching && !projectStore.searchResults.length && !msgResults.length && searchText" class="search-hint">无匹配结果</div>
    </div>

    <div v-if="!collapsed" class="plist">
      <div
        v-for="p in projectStore.projects"
        :key="p.id"
        class="pitem"
        :class="{ active: p.id === projectStore.currentProjectId }"
        @click="selectProject(p.id)"
      >
        📁 {{ p.name }}
      </div>
      <div v-if="projectStore.projects.length === 0" class="empty">暂无项目，点 ＋ 新建</div>
    </div>
    <div v-else class="plist collapsed">
      <div
        v-for="p in projectStore.projects"
        :key="p.id"
        class="pdot"
        :class="{ active: p.id === projectStore.currentProjectId }"
        @click="selectProject(p.id)"
      >
        📁
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 220px;
  border-right: 1px solid var(--border);
  background: var(--panel);
  display: flex;
  flex-direction: column;
  min-height: 0;
  transition: width var(--transition-smooth);
  box-shadow: var(--shadow-sm);
  position: relative;
  z-index: 2;
}
.sidebar.collapsed {
  width: 52px;
}
.actions {
  display: flex;
  gap: 6px;
  padding: 10px;
  border-bottom: 1px solid var(--border);
}
.act {
  flex: 1;
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: var(--radius-sm);
  padding: 8px 0;
  cursor: pointer;
  font-size: 14px;
  color: var(--text-secondary);
  transition: all var(--transition-fast);
}
.act:hover {
  background: var(--hover-bg);
  border-color: var(--brand);
  color: var(--brand);
}
.act.toggle-btn {
  flex: 0;
  font-size: 16px;
  padding: 8px 10px;
}
.sidebar.collapsed .actions {
  flex-direction: column;
  gap: 4px;
}
.searchbox {
  padding: 8px;
  border-bottom: 1px solid var(--border);
}
.searchbox input {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 5px 8px;
  font-size: 13px;
}
.sres {
  margin-top: 6px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  max-height: 200px;
  overflow: auto;
}
.sitem {
  padding: 6px 8px;
  font-size: 13px;
  cursor: pointer;
}
.sitem:hover {
  background: #f3f4f6;
}
.sres-label {
  font-size: 10px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  padding: 6px 8px 2px;
  border-bottom: 1px solid var(--border);
}
.search-hint {
  text-align: center;
  color: var(--muted);
  font-size: 12px;
  padding: 10px 0;
}
/* 消息搜索结果 */
.msg-results {
  max-height: 300px;
}
.msg-item {
  line-height: 1.4;
  border-bottom: 1px solid var(--border);
}
.msg-item:last-child { border-bottom: none; }
.msg-meta {
  font-size: 10px;
  color: var(--muted);
  margin-bottom: 2px;
}
.msg-user {
  font-size: 12px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.msg-ai {
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plist {
  flex: 1;
  overflow: auto;
  padding: 8px;
}
.pitem {
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  margin-bottom: 4px;
}
.pitem:hover {
  background: var(--hover-bg);
  color: var(--text);
}
.pitem.active {
  background: var(--brand-bg);
  color: var(--brand);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--brand);
}
.empty {
  color: var(--muted);
  font-size: var(--text-sm);
  padding: 12px;
  text-align: center;
}
.pdot {
  text-align: center;
  padding: 8px 0;
  cursor: pointer;
}
.pdot.active {
  background: #eef2ff;
  border-radius: 8px;
}
</style>
