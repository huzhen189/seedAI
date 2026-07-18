<script setup lang="ts">
import { onMounted } from 'vue'
import { useProjectStore } from '../stores/project'

const projectStore = useProjectStore()

onMounted(() => projectStore.load())

async function del(id: number) {
  if (confirm('确认删除该项目及其下所有会话？')) {
    await projectStore.remove(id)
  }
}
</script>

<template>
  <div class="projects">
    <h2>我的项目</h2>
    <ul>
      <li v-for="p in projectStore.projects" :key="p.id">
        <span>📁 {{ p.name }}</span>
        <button @click="del(p.id)">删除</button>
      </li>
    </ul>
    <p v-if="projectStore.projects.length === 0" class="empty">暂无项目，去对话页左侧 ＋ 新建。</p>
  </div>
</template>

<style scoped>
.projects {
  flex: 1;
  padding: 24px;
  overflow: auto;
}
.projects h2 {
  margin: 0 0 16px;
  font-size: 18px;
}
.projects ul {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.projects li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
}
.projects li button {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 8px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
  color: var(--err);
}
.empty {
  color: var(--muted);
  font-size: 13px;
}
</style>
