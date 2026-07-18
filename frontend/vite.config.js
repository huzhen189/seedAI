import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
// 前端在 :3000 开发;所有 /api 请求代理到业务服务(:8000)。
// 生产由 nginx(同域)或业务服务托管静态产物,无需代理。
export default defineConfig({
    plugins: [vue()],
    server: {
        port: 3000,
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
        },
    },
});
