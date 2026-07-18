import { createRouter, createWebHistory } from 'vue-router'
import ChatView from '../views/ChatView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import SettingsView from '../views/SettingsView.vue'
import AdminView from '../views/AdminView.vue'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatView },
    { path: '/projects', name: 'projects', component: ProjectsView },
    { path: '/settings', name: 'settings', component: SettingsView },
    // 管理后台(RBAC 三级:仅 admin / super_admin 可进,见《业务端细节》§3)
    {
      path: '/admin',
      name: 'admin',
      component: AdminView,
      meta: { requiresAdmin: true },
    },
  ],
})

// 路由守卫:管理后台仅对 admin / super_admin 开放,其余一律回首页。
router.beforeEach((to, _from, next) => {
  if (to.meta.requiresAdmin) {
    const role = useAuthStore().user?.role
    if (role === 'admin' || role === 'super_admin') next()
    else next('/')
  } else {
    next()
  }
})

export default router
