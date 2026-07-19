/** 前端性能追踪(客户端上报 → 后端 /admin/analytics/perf)。 */
export function initPerfTracking(): void {
  if (typeof window === 'undefined' || !window.performance) return

  // 等页面完全加载后上报(避免阻塞首次渲染)
  window.addEventListener('load', () => {
    setTimeout(() => {
      try {
        const nav = window.performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
        if (!nav) return
        const payload: Record<string, number> = {}
        // TTFB
        if (nav.responseStart > 0) {
          payload.ttfb = nav.responseStart - nav.requestStart
        }
        // DOM 就绪
        if (nav.domContentLoadedEventEnd > 0) {
          payload.dom_ready = nav.domContentLoadedEventEnd - nav.fetchStart
        }
        // 全页面加载
        if (nav.loadEventEnd > 0) {
          payload.page_load = nav.loadEventEnd - nav.fetchStart
        }
        // TCP + SSL + DNS 等网络耗时(仅统计,不单独展示)
        if (nav.connectEnd > 0 && nav.connectStart > 0) {
          payload.network = nav.connectEnd - nav.connectStart
        }
        const hasData = Object.values(payload).some(v => v > 0)
        if (!hasData) return
        // 发送到后端(fire-and-forget, 不计鉴权)
        navigator.sendBeacon('/admin/analytics/perf', JSON.stringify(payload))
      } catch {
        /* 静默 */
      }
    }, 300) // 延迟 300ms 确保 load 事件处理完
  })
}
