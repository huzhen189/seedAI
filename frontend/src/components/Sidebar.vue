<script setup lang="ts">
import { ref } from 'vue'
import { useProjectStore } from '../stores/project'

defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ toggle: [] }>()
const projectStore = useProjectStore()

const showSearch = ref(false)
const searchText = ref('')

async function newProject() {
  const name = prompt('项目名称：', '我的项目')
  if (!name) return
  await projectStore.create(name)
}
function selectProject(id: number) {
  projectStore.currentProjectId = id
}
async function onSearch() {
  await projectStore.search(searchText.value)
}
function pickSearch(item: any) {
  searchText.value = ''
  projectStore.searchResults = []
  if (item.type === 'project') projectStore.currentProjectId = item.id
  else if (item.project_id != null) projectStore.currentProjectId = item.project_id
  showSearch.value = false
}
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
      <input v-model="searchText" placeholder="搜索项目 / 会话" @input="onSearch" />
      <div v-if="projectStore.searchResults.length" class="sres">
        <div
          v-for="r in projectStore.searchResults"
          :key="r.type + r.id"
          class="sitem"
          @click="pickSearch(r)"
        >
          {{ r.type === 'project' ? '📁' : '💬' }} {{ r.title }}
        </div>
      </div>
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
  width: 210px;
  border-right: 1px solid var(--border);
  background: var(--panel);
  display: flex;
  flex-direction: column;
  min-height: 0;
  transition: width 0.15s;
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
  background: #fff;
  border-radius: 8px;
  padding: 6px 0;
  cursor: pointer;
  font-size: 14px;
}
.act.toggle-btn {
  flex: 0;
  font-size: 18px;
  font-weight: bold;
  padding: 8px 12px;
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
  background: #f3f4f6;
}
.pitem.active {
  background: #eef2ff;
  color: var(--brand);
  font-weight: 600;
}
.empty {
  color: var(--muted);
  font-size: 12px;
  padding: 10px;
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
