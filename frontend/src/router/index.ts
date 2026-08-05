import { createRouter, createWebHistory } from 'vue-router'
import ChatView from '../views/ChatView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import SettingsView from '../views/SettingsView.vue'
import AdminView from '../views/AdminView.vue'
import { useAuthStore } from '../stores/auth'
import { trackPageView } from '../composables/track'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatView },
    // 其余业务/管理路由均需登录(无 Cookie 直接回首页, 杜绝"未登录能进 admin/settings")
    { path: '/projects', name: 'projects', component: ProjectsView, meta: { requiresAuth: true } },
    { path: '/settings', name: 'settings', component: SettingsView, meta: { requiresAuth: true } },
    // 管理后台(RBAC 三级:仅 admin / super_admin 可进,见《业务端细节》§3)
    {
      path: '/admin',
      name: 'admin',
      component: AdminView,
      meta: { requiresAuth: true, requiresAdmin: true },
    },
    // 兜底: 未知路径回首页
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

// 路由守卫: 未登录访问任何受保护路由 → 回首页并弹登录;
// 非 admin/super_admin 访问 /admin → 回首页。守卫内 await init 确保
// 读到的 auth.user 是鉴权后的真实态(首屏未 init 完成时也不会误放行/误拦)。
router.beforeEach(async (to, _from, next) => {
  const auth = useAuthStore()
  await auth.init()
  if (to.meta.requiresAuth && !auth.user) {
    auth.openLogin()
    next('/')
    return
  }
  if (to.meta.requiresAdmin) {
    const role = auth.user?.role
    if (role === 'admin' || role === 'super_admin') next()
    else next('/')
    return
  }
  next()
})

// 路由切换后上报页面访问(STAT-3: 前端访问统计)
router.afterEach((to) => {
  trackPageView(to.path)
})

export default router
