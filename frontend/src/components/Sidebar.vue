<script setup lang="ts">
import { ref, watch } from 'vue'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import {
  listConversations, renameProject, renameConversation, searchMessages,
  type MessageSearchResult,
} from '../api/projects'
import type { Conversation } from '../types'

defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{
  toggle: []
  'navigate-message': [result: MessageSearchResult]
}>()
const projectStore = useProjectStore()
const convStore = useConversationStore()

const showSearch = ref(false)
const searchText = ref('')
const msgResults = ref<MessageSearchResult[]>([])
const searching = ref(false)

// ── 项目树展开状态 + 懒加载会话 ──
const expanded = ref<Record<number, boolean>>({})
const tree = ref<Record<number, Conversation[]>>({})
const loadingTree = ref<Record<number, boolean>>({})

async function toggleProject(id: number) {
  if (expanded.value[id]) {
    expanded.value[id] = false
    return
  }
  expanded.value[id] = true
  if (!tree.value[id]) {
    loadingTree.value[id] = true
    try {
      tree.value[id] = await listConversations(id)
    } finally {
      loadingTree.value[id] = false
    }
  }
}

function selectProject(id: number) {
  projectStore.currentProjectId = id
}

async function openConv(conv: Conversation) {
  projectStore.currentProjectId = conv.project_id
  await convStore.openConversation(conv.id)
}

// ── 双击就地改名 ──
const editingProjectId = ref<number | null>(null)
const editProjectName = ref('')
const editingConvId = ref<number | null>(null)
const editConvName = ref('')

function startEditProject(p: { id: number; name: string }) {
  editingProjectId.value = p.id
  editProjectName.value = p.name
}
async function saveProjectName() {
  const id = editingProjectId.value
  if (id == null) return
  const name = editProjectName.value.trim()
  editingProjectId.value = null
  if (name && name !== projectStore.projects.find(p => p.id === id)?.name) {
    await renameProject(id, name)
    await projectStore.load()
  }
}
function startEditConv(c: Conversation) {
  editingConvId.value = c.id
  editConvName.value = c.name || ''
}
async function saveConvName() {
  const id = editingConvId.value
  if (id == null) return
  const name = editConvName.value.trim()
  editingConvId.value = null
  const pid = cachedPid.value
  const oldName = pid != null ? (tree.value[pid]?.find((x: Conversation) => x.id === id)?.name) : undefined
  if (name && name !== oldName) {
    await renameConversation(id, name)
    if (pid != null) tree.value[pid] = await listConversations(pid)
  }
}
// 记住正在编辑的会话所属项目, 改名后刷新对应树
const cachedPid = ref<number | null>(null)
function startEditConvWithPid(c: Conversation, pid: number) {
  cachedPid.value = pid
  startEditConv(c)
}

// ── 新建项目 ──
async function newProject() {
  const name = prompt('项目名称：', '我的项目')
  if (!name) return
  await projectStore.create(name)
}

// ── 搜索 ──
let _searchTimer: ReturnType<typeof setTimeout> | null = null
async function onSearch() {
  const q = searchText.value.trim()
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
  sessionStorage.setItem('nav_conv', String(r.conversation_id))
  sessionStorage.setItem('nav_msg', String(r.message_id))
  projectStore.currentProjectId = r.project_id
}

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

    <!-- 项目树: 项目 → 会话, 双击改名 -->
    <div v-if="!collapsed" class="plist">
      <div v-for="p in projectStore.projects" :key="p.id" class="ptree">
        <div
          class="pitem"
          :class="{ active: p.id === projectStore.currentProjectId }"
          @click="selectProject(p.id)"
        >
          <span class="caret" :class="{ open: expanded[p.id] }" @click.stop="toggleProject(p.id)">▸</span>
          <span
            v-if="editingProjectId !== p.id"
            class="pname"
            title="双击改名"
            @dblclick.stop="startEditProject(p)"
          >📁 {{ p.name }}</span>
          <input
            v-else
            v-model="editProjectName"
            class="rename-input"
            @keyup.enter="saveProjectName"
            @keyup.escape="editingProjectId = null"
            @blur="saveProjectName"
          />
        </div>

        <div v-if="expanded[p.id]" class="ctree">
          <div v-if="loadingTree[p.id]" class="loading-more">加载会话…</div>
          <template v-else>
            <div
              v-for="c in (tree[p.id] || [])"
              :key="c.id"
              class="citem"
              :class="{ active: c.id === convStore.currentConvId }"
              @click="openConv(c)"
            >
              <span
                v-if="editingConvId !== c.id"
                class="cname"
                title="双击改名"
                @dblclick.stop="startEditConvWithPid(c, p.id)"
              >💬 {{ c.name || '会话' }}</span>
              <input
                v-else
                v-model="editConvName"
                class="rename-input"
                @keyup.enter="saveConvName"
                @keyup.escape="editingConvId = null"
                @blur="saveConvName"
              />
            </div>
            <div v-if="!(tree[p.id] || []).length" class="empty small">暂无会话</div>
          </template>
        </div>
      </div>
      <div v-if="projectStore.projects.length === 0" class="empty">暂无项目，点 ＋ 新建</div>
    </div>
    <div v-else class="plist collapsed">
      <div
        v-for="p in projectStore.projects"
        :key="p.id"
        class="pdot"
        :class="{ active: p.id === projectStore.currentProjectId }"
        @click="toggleProject(p.id)"
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
  background: #eef2ef;
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
.ptree { margin-bottom: 2px; }
.pitem {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
}
.pitem:hover { background: var(--hover-bg); color: var(--text); }
.pitem.active {
  background: var(--brand-bg);
  color: var(--brand);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--brand);
}
.caret {
  display: inline-block;
  font-size: 10px;
  color: var(--muted);
  transition: transform 0.15s ease;
  width: 10px;
}
.caret.open { transform: rotate(90deg); }
.pname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctree {
  margin: 2px 0 6px 18px;
  border-left: 1px dashed var(--border);
  padding-left: 6px;
}
.citem {
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  color: var(--text-secondary);
}
.citem:hover { background: var(--hover-bg); color: var(--text); }
.citem.active {
  background: var(--brand-bg);
  color: var(--brand);
  font-weight: 600;
}
.cname { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rename-input {
  flex: 1;
  width: 100%;
  border: 1px solid var(--brand);
  border-radius: 6px;
  padding: 3px 6px;
  font-size: 13px;
  outline: none;
}
.empty {
  color: var(--muted);
  font-size: var(--text-sm);
  padding: 12px;
  text-align: center;
}
.empty.small { padding: 4px 8px; font-size: 12px; }
.pdot {
  text-align: center;
  padding: 8px 0;
  cursor: pointer;
}
.pdot.active {
  background: #def6ef;
  border-radius: 8px;
}
</style>
