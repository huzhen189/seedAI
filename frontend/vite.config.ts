import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import dns from 'node:dns'
import fs from 'node:fs'
import path from 'node:path'

// 强制 Node.js DNS 解析优先返回 IPv4。否则 localhost 可能被解析为 ::1,
// 代理连接 uvicorn(仅监听 IPv4 0.0.0.0)时触发 ECONNREFUSED ::1:7101。
// 与下方 proxy target 使用 127.0.0.1(字面 IPv4)形成双保险。
dns.setDefaultResultOrder('ipv4first')

// 前端在 :7100 开发;所有 /api 请求代理到业务服务(:7101)。
// 生产由 nginx(同域)或业务服务托管静态产物,无需代理。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    port: 7100,
    // WebLLM 需要 SharedArrayBuffer → 跨域隔离头(仅生产 HTTPS 生效,本地 HTTP 会触发 COOP 忽略警告)
    // v1.0: dev 模式移除, 本地用 HTTP 不需要且会干扰 fetch
    // headers: {
    //   'Cross-Origin-Opener-Policy': 'same-origin',
    //   'Cross-Origin-Embedder-Policy': 'require-corp',
    // },
    // Vite 5.4+ 默认拦截非 localhost 的 Host 头(防 DNS 重绑定),
    // 本地 dev 用自定义域名访问需关闭该检查(仅本地开发,生产走 nginx 不受影响)。
    allowedHosts: true,
    // P1: 本地产物静态直出。nginx 已配 /artifacts/ alias, 但若用户直连 vite(:7100) 而非
    // 经 nginx 域名访问, 需在 dev 中间件补一份静态服务, 保证预览同源可加载。
    // 生产由 nginx 处理, 此中间件仅 dev 生效(server.middlewareMode=false 时即为 dev)。
    setupMiddlewares(middlewares, devServer) {
      middlewares.use('/artifacts', async (req, res, next) => {
        try {
          const rel = decodeURIComponent((req.url || '').split('?')[0].replace(/^\/+/, ''))
          // 防目录穿越: 仅允许相对 ARTIFACT_DIR 内部路径
          const safe = path.normalize(rel).replace(/^(\.\.(\/|\\|$))+/, '')
          const root = path.resolve(__dirname, '..', 'artifacts')
          const target = path.resolve(root, safe)
          if (!target.startsWith(root) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
            res.statusCode = 404
            res.end('Not found')
            return
          }
          // q-0 兜底校验: 直连 vite 时(不经 nginx auth_request)自行调用后端鉴权端点,
          // 避免软删/回收区/非 owner 私有项目直链越权读取。公开项目/owner 放行, 其余拒。
          if (!safe.startsWith('.trash/')) {
            try {
              const authUrl = (process.env.VITE_API_TARGET || 'http://127.0.0.1:7101') +
                '/api/artifacts-auth?path=' + encodeURIComponent(safe)
              const authResp = await fetch(authUrl, {
                method: 'GET',
                headers: { Cookie: req.headers.cookie || '' },
                redirect: 'manual',
              })
              if (authResp.status !== 200) {
                res.statusCode = authResp.status === 401 ? 401 : 403
                res.setHeader('Content-Type', 'application/json')
                res.end(JSON.stringify({ detail: 'forbidden' }))
                return
              }
            } catch {
              // 鉴权端点不可达 → 宁拒勿放(防止 dev 环境绕过), 但允许 .trash 已排除
              res.statusCode = 403
              res.end('Auth check failed')
              return
            }
          } else {
            // 回收区内容不允许经静态直出
            res.statusCode = 403
            res.end('Forbidden')
            return
          }
          const ext = path.extname(target).toLowerCase()
          const mime: Record<string, string> = {
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.md': 'text/markdown; charset=utf-8',
            '.txt': 'text/plain; charset=utf-8',
          }
          res.setHeader('Content-Type', mime[ext] || 'application/octet-stream')
          res.setHeader('Cache-Control', 'public, max-age=3600')
          fs.createReadStream(target).pipe(res)
        } catch {
          res.statusCode = 500
          res.end('Error')
        }
      })
      return middlewares
    },
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
