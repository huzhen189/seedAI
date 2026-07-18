import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前端在 :7100 开发;所有 /api 请求代理到业务服务(:7101)。
// 生产由 nginx(同域)或业务服务托管静态产物,无需代理。
export default defineConfig({
  plugins: [vue()],
  server: {
    // host:true 允许通过域名(如 seedai.huzhen.net.cn)/局域网 IP 访问 dev server,
    // 否则 vite 默认只绑 localhost,域名访问会被拒。
    host: true,
    port: 7100,
    proxy: {
      '/api': {
        // 默认指向业务服务(7101);如需覆盖可用
        // VITE_API_TARGET=http://localhost:xxxx npm run dev
        target: process.env.VITE_API_TARGET || 'http://localhost:7101',
        changeOrigin: true,
      },
      // 登录/注册等鉴权接口也代理到业务服务(同源,Cookie 可随请求自动携带)
      '/auth': {
        target: process.env.VITE_API_TARGET || 'http://localhost:7101',
        changeOrigin: true,
      },
    },
  },
})
