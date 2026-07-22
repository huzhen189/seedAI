/** 前端访问 / 点击追踪(STAT-3): 上报到后端 /admin/analytics/track。

- trackPageView(route): 路由切换时上报页面访问(由 router.afterEach 调用);
- trackClick(label): 点击带 data-track 的元素时上报;
- initTracking(): 安装全局点击监听,自动捕获带 data-track 属性的元素(含最近祖先),
  无需在每个按钮上写逻辑。

上报使用 navigator.sendBeacon(fire-and-get),失败静默,不影响主流程。
*/

let installed = false

function beacon(payload: Record<string, unknown>): void {
  try {
    const data = JSON.stringify(payload)
    if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
      navigator.sendBeacon('/admin/analytics/track', data)
    } else {
      fetch('/admin/analytics/track', {
        method: 'POST',
        body: data,
        headers: { 'Content-Type': 'application/json' },
        keepalive: true,
      }).catch(() => {
        /* 静默 */
      })
    }
  } catch {
    /* 静默 */
  }
}

/** 上报页面访问(路由路径)。 */
export function trackPageView(route: string): void {
  beacon({ type: 'page_view', route: route || 'unknown' })
}

/** 上报一次点击(标签 <= 60 字)。 */
export function trackClick(label: string): void {
  const l = (label || '').trim()
  if (!l) return
  beacon({ type: 'click', label: l.slice(0, 60) })
}

/** 安装全局点击埋点(幂等)。捕获最近带 [data-track] 的元素。 */
export function initTracking(): void {
  if (installed || typeof document === 'undefined') return
  installed = true
  document.addEventListener(
    'click',
    (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      if (!target) return
      const el = target.closest('[data-track]') as HTMLElement | null
      if (!el) return
      const label =
        el.getAttribute('data-track') ||
        el.getAttribute('aria-label') ||
        (el.textContent || '').trim().slice(0, 30)
      if (label) trackClick(label)
    },
    true,
  )
}
