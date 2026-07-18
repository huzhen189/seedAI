import { createRouter, createWebHistory } from 'vue-router'
import ChatView from '../views/ChatView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import SettingsView from '../views/SettingsView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatView },
    { path: '/projects', name: 'projects', component: ProjectsView },
    { path: '/settings', name: 'settings', component: SettingsView },
  ],
})

export default router
