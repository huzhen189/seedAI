import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import dns from 'node:dns'
import fs from 'node:fs'
import path from 'node:path'

// 强制 Node.js DNS 解析优先返回 IPv4。否则 localhost 可能被解析为 ::1,
// 代理连接 uvicorn(仅监听 IPv4 0.0.0.0)时触发 ECONNREFUSED ::1:7101。
// 与下方 proxy target 使用 127.0.0.1(字面 IPv4)形成双保险。
dns.setDefaultResultOrder('ipv4first')

// 通用本地静态根中间件工厂: 把 url 前缀 baseUrl(/artifacts 或 /vendor)映射到本地磁盘 root 目录。
// dev 与 preview 共用,保证生成产物 / 组件库同源可加载。
// 生产由 nginx 处理(alias),此中间件仅在 vite dev/preview 生效。
function addStaticRootMiddleware(middlewares: any, baseUrl: string, root: string, opts: any = {}) {
  middlewares.use(baseUrl, async (req: any, res: any, next: any) => {
    try {
      const rel = decodeURIComponent((req.url || '').split('?')[0].replace(/^\/+/, ''))
      // 防目录穿越: 仅允许相对 root 内部路径
      const safe = path.normalize(rel).replace(/^(\.\.(\/|\\|$))+/, '')
      const target = path.resolve(root, safe)
      if (!target.startsWith(root) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
        res.statusCode = 404
        res.end('Not found')
        return
      }
      // /artifacts 私有产物需鉴权; /vendor 为公开共享库, 无需鉴权。
      if (baseUrl === '/artifacts' && !safe.startsWith('.trash/')) {
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
          // 鉴权端点不可达 → 宁拒勿放(防止 dev 环境绕过)
          res.statusCode = 403
          res.end('Auth check failed')
          return
        }
      } else if (baseUrl === '/artifacts' && safe.startsWith('.trash/')) {
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
}

// API 代理目标(本机业务服务)
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:7101'
const apiProxy = {
  '/api': { target: apiTarget, changeOrigin: true, ws: false },
  '/auth': { target: apiTarget, changeOrigin: true, ws: false },
  '/admin': { target: apiTarget, changeOrigin: true, ws: false },
}

// 前端在 :7100 开发;所有 /api 请求代理到业务服务(:7101)。
// 生产由 nginx(同域)或业务服务托管静态产物,无需代理。
export default defineConfig({
  plugins: [
    vue(),
    // P1: 本地产物 / 本地组件库静态直出。nginx 已配 /artifacts 与 /vendor 的 alias;
    // 但若用户直连 vite(:7100 dev 或 :8080 preview)而非经 nginx 域名访问, 需在 dev/preview 中间件补一份静态服务,
    // 保证预览同源可加载(生成站点引用的 /vendor/libs/... 也能在预览期正确加载)。
    // Vite 5 中 configureServer / configurePreviewServer 必须为 plugin hook(不能嵌在 server/preview 内)。
    {
      name: 'seedai-static-middleware',
      configureServer(api) {
        addStaticRootMiddleware(api.middlewares, '/artifacts', path.resolve(__dirname, '..', 'artifacts'))
        addStaticRootMiddleware(api.middlewares, '/vendor', path.resolve(__dirname, '..', 'backend', 'shared', 'vendor'))
      },
      configurePreviewServer(api) {
        addStaticRootMiddleware(api.middlewares, '/artifacts', path.resolve(__dirname, '..', 'artifacts'))
        addStaticRootMiddleware(api.middlewares, '/vendor', path.resolve(__dirname, '..', 'backend', 'shared', 'vendor'))
      },
    },
  ],
  server: {
    host: true,
    port: 7100,
    // Vite 5.4+ 默认拦截非 localhost 的 Host 头(防 DNS 重绑定),
    // 本地 dev 用自定义域名访问需关闭该检查(仅本地开发,生产走 nginx 不受影响)。
    allowedHosts: true,
    proxy: apiProxy,
  },
  // 生产构建产物预览(serve frontend/dist): 本机未起 nginx 时,用此预览服务访问真实构建产物。
  // 与 dev(7100) 互不干扰。启动: npm run preview (或 npx vite preview --port 8080)
  preview: {
    host: true,
    port: 8080,
    proxy: apiProxy,
  },
})
