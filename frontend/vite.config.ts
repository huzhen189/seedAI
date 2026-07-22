import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前端在 :7100 开发;所有 /api 请求代理到业务服务(:7101)。
// 生产由 nginx(同域)或业务服务托管静态产物,无需代理。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    port: 7100,
    // WebLLM 需要 SharedArrayBuffer → 跨域隔离头
    headers: {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
    // Vite 5.4+ 默认拦截非 localhost 的 Host 头(防 DNS 重绑定),
    // 本地 dev 用自定义域名访问需关闭该检查(仅本地开发,生产走 nginx 不受影响)。
    allowedHosts: true,
    proxy: {
      '/api': {
        // 默认指向业务服务(7101);使用 127.0.0.1 而非 localhost,避免 Node.js DNS
        // 优先解析 IPv6 ::1 导致连接失败(uvicorn 默认只监听 IPv4 0.0.0.0)。
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:7101',
        changeOrigin: true,
        ws: false, // /api 无需 WebSocket,禁用避免升级冲突
      },
      // 管理后台(§10):/admin/* 同样代理到业务服务(同源,Cookie 随请求自动携带)
      '/admin': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:7101',
        changeOrigin: true,
        ws: false,
      },
      // 登录/注册等鉴权接口也代理到业务服务(同源,Cookie 可随请求自动携带)
      '/auth': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:7101',
        changeOrigin: true,
        ws: false,
      },
    },
  },
})
