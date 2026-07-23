<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { get, post } from '../api/client'
import { useAuthStore } from '../stores/auth'
import RadarChart from '../components/RadarChart.vue'
import { ROLE_LABELS, QC_DIM_LABELS, type AdminUser, type MetricsSnapshot, type Role } from '../types'

const auth = useAuthStore()
const isSuper = computed(() => auth.user?.role === 'super_admin')
const currentRoleLabel = computed(
  () => ROLE_LABELS[(auth.user?.role as Role) || 'user'] || auth.user?.role || '-',
)

// ---- 标签页(RBAC:用户管理 / 控制面 仅超管可见) ----
type Tab = 'metrics' | 'users' | 'control' | 'quality' | 'replay' | 'analytics'
const tabs: { key: Tab; label: string; superOnly: boolean }[] = [
  { key: 'metrics', label: '运行指标', superOnly: false },
  { key: 'quality', label: 'AI 质量', superOnly: false },
  { key: 'replay', label: '回放', superOnly: false },
  { key: 'analytics', label: '系统分析', superOnly: false },
  { key: 'users', label: '用户管理', superOnly: true },
  { key: 'control', label: '控制面', superOnly: true },
]
const activeTab = ref<Tab>('metrics')
const visibleTabs = computed(() => tabs.filter((t) => !t.superOnly || isSuper.value))

// ---- 实时指标(SSE /admin/metrics) ----
const metrics = ref<MetricsSnapshot>({})
let es: EventSource | null = null

function connectMetrics() {
  es = new EventSource('/admin/metrics')
  es.addEventListener('metrics', (e) => {
    try {
      metrics.value = JSON.parse((e as MessageEvent).data)
    } catch {
      /* 忽略坏帧 */
    }
  })
  // 后端每 2s 推送;断开由 EventSource 自动重连,指标保留上次值即可。
}

function fmtUptime(s?: number): string {
  if (s == null) return '-'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const parts: string[] = []
  if (d) parts.push(`${d}天`)
  if (h) parts.push(`${h}时`)
  parts.push(`${m}分`)
  return parts.join('')
}

const totalModelUsage = computed(() => {
  const u = metrics.value.model_usage || {}
  return Object.values(u).reduce((a, b) => a + (b || 0), 0)
})

// ---- 用户管理(仅超管) ----
const users = ref<AdminUser[]>([])
const usersLoading = ref(false)

async function fetchUsers() {
  usersLoading.value = true
  try {
    users.value = await get('/admin/users')
  } catch {
    /* 忽略 */
  } finally {
    usersLoading.value = false
  }
}

async function changeRole(u: AdminUser, role: string) {
  try {
    const updated = await post(`/admin/users/${u.id}/role`, { role })
    u.role = updated.role
  } catch (e: any) {
    if (e?.message !== 'AUTH_REQUIRED') alert(e?.message || '网络错误')
  }
}

async function changePlan(u: AdminUser, plan: string) {
  try {
    const updated = await post(`/admin/users/${u.id}/plan`, { plan })
    u.plan = updated.plan
  } catch (e: any) {
    if (e?.message !== 'AUTH_REQUIRED') alert(e?.message || '网络错误')
  }
}

// 是否禁用该行的角色选择(超管不可被改;不可改自己)
function roleDisabled(u: AdminUser): boolean {
  if (u.role === 'super_admin') return true
  if (u.id === auth.user?.id) return true
  return false
}

// ---- 控制面(仅超管) ----
const scaleName = ref('ai_service')
const scaleReplicas = ref(2)
const stopName = ref('ai_service')
const ctrlMsg = ref('')

// ---- 重置系统(超管) ----
const resetLoading = ref(false)
const resetMsg = ref('')
async function doReset() {
   
  const ok = confirm('⚠ 此操作将清空全部数据库记录 + Redis 缓存。系统将重建表并创建默认超管用户 huzhen。前端本地数据也将一并清除。是否继续？')
  if (!ok) return
  resetLoading.value = true
  resetMsg.value = ''
  try {
    // 1) 清理前端本地数据
    localStorage.clear()
    sessionStorage.clear()
    if (window.indexedDB?.databases) {
      try {
        const dbs = await window.indexedDB.databases()
        for (const db of dbs) { if (db.name) window.indexedDB.deleteDatabase(db.name) }
      } catch { /* IndexedDB 清理静默忽略 */ }
    }
    // 2) 调后端清库
    const r = await post('/admin/reset?confirm=yes')
    if (r.success) {
      resetMsg.value = `✅ ${r.message}\n已 DROP ${r.tables_dropped} 张表, Redis ${r.redis_cleared ? '已清空' : '清理失败'}。\n请立即重启两个后端服务(业务 7101 + AI 7102)，刷新本页面重新登录。`
    } else {
      resetMsg.value = `❌ 重置失败: ${r.error || '未知错误'}`
    }
  } catch (e: unknown) {
    resetMsg.value = `❌ 网络错误: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    resetLoading.value = false
  }
}

// ---- DB 状态展示(类型桥接 v-for) ----
interface DbItem { key: string; ok: boolean; error?: string; pool_size?: number; checked_in?: number; overflow?: number }
const dbItems = computed<DbItem[]>(() => {
  const db = metrics.value.db
  if (!db) return []
  return Object.entries(db).map(([key, info]) => ({ key, ...(info as any) }))
})

async function doScale() {
  try {
    const d = await post(
      `/admin/scale?name=${encodeURIComponent(scaleName.value)}&replicas=${scaleReplicas.value}`,
    )
    ctrlMsg.value = d.log || (d.ack ? '已提交扩缩容' : '操作失败')
  } catch {
    ctrlMsg.value = '网络错误'
  }
}
async function doStop() {
  try {
    const d = await post(`/admin/stop?name=${encodeURIComponent(stopName.value)}`)
    ctrlMsg.value = d.log || (d.ack ? '已提交停止' : '操作失败')
  } catch {
    ctrlMsg.value = '网络错误'
  }
}

// ---- AI 质量(③-a) ----
interface QualityData {
  feedback_count: number
  avg_rating: number | null
  rating_distribution: Record<number, number>
  model_usage: Record<string, number>
  reviewer_pass_rate: number
  reviewer_total: number
  generation_total: number
  generation_success_rate: number
  unsupported_count?: number
  // QC 三裁判聚合(v0.8.5 M1)
  qc_count: number
  qc_overall_avg: number | null
  qc_overall_dim_avg: Record<string, number>
  qc_model_avg: Record<string, Record<string, number>>
  qc_review_rate: number
  qc_dimensions: string[]
  qc_dim_labels: Record<string, string>
  qc_judges: string[]
}
const quality = ref<QualityData | null>(null)
const qualityLoading = ref(false)

async function fetchQuality() {
  qualityLoading.value = true
  try {
    quality.value = await get('/admin/quality')
  } catch { /* ignore */ }
  finally { qualityLoading.value = false }
}

// QC 雷达图序列: 3 模型 + 整体(各 6 维)
const qcSeries = computed(() => {
  const q = quality.value
  if (!q || !q.qc_dimensions?.length) return []
  const axes = q.qc_dimensions
  const mk = (name: string, color: string, src: Record<string, number>) => ({
    name, color, values: axes.map((d) => Number(src?.[d] ?? 0)),
  })
  return [
    mk('DeepSeek', '#2563eb', q.qc_model_avg?.['deepseek'] || {}),
    mk('Qwen', '#16a34a', q.qc_model_avg?.['qwen'] || {}),
    mk('HY3', '#d97706', q.qc_model_avg?.['hy3'] || {}),
    mk('整体', '#7c3aed', q.qc_overall_dim_avg || {}),
  ]
})

// 维度 key -> 中文标签(兼容任意字符串 key, 避免模板内严格索引报错)
function qcLabel(d: string): string {
  return (QC_DIM_LABELS as Record<string, string>)[d] || d
}

// ---- 回放(③-a) ----
interface TraceItem {
  id: number; trace_id: string; user_id: number; model_id: string | null
  status: string; total_tokens: number; started_at: string | null; finished_at: string | null
  qc_overall?: number | null
  feedback_rating?: number | null
}
interface TraceEventItem {
  seq: number; event_type: string; stage: string | null
  payload: unknown; created_at: string | null
}
interface QcDetail { overall: number; result: any; needs_review: boolean; safety_risk: string; partial: boolean; created_at: string | null }
interface FeedbackDetail { rating: number; comment: string | null; dimensions: any; created_at: string | null }
interface TraceMessage { role: string; model_id: string | null; content: string; created_at: string | null }
interface TraceDetail {
  trace: TraceItem
  events: TraceEventItem[]
  qc?: QcDetail | null
  feedback?: FeedbackDetail | null
  messages?: TraceMessage[]
}
const traces = ref<TraceItem[]>([])
const tracesLoading = ref(false)
const selectedTrace = ref<TraceDetail | null>(null)

async function fetchTraces() {
  tracesLoading.value = true
  try {
    traces.value = await get('/admin/traces?limit=50')
  } catch { /* ignore */ }
  finally { tracesLoading.value = false }
}

async function viewTrace(traceId: string) {
  try {
    selectedTrace.value = await get(`/admin/traces/${traceId}`)
  } catch { /* ignore */ }
}

// ---- 系统分析(命中率/准确率/响应时间/前端性能) ----
interface LatencyBucket { p50: number; p90: number; p99: number; avg: number; samples: number }
interface IntentStat { ok: number; total: number; rate: number }
interface SkillStat { ok: number; fail: number; abort: number; total: number; success_rate: number }
interface ApiCallStat {
  total: number
  ok: number
  fail: number
  success_rate: number
  latency: LatencyBucket
}
interface OrchestrationStat {
  total: number
  available: boolean
  strategy_dist?: Record<string, number>
  split_count?: LatencyBucket
  success_rate?: LatencyBucket
  duration_ms?: LatencyBucket
  sub_tasks?: {
    total: number
    status_dist: Record<string, number>
    risk_dist: Record<string, number>
    per_skill: Record<string, { total: number; done: number; failed: number; blocked: number; skipped: number; success_rate: number }>
    duration_ms: LatencyBucket
  }
}
interface AnalyticsSnapshot {
  intent_stats: Record<string, IntentStat>
  skill_outcomes: Record<string, SkillStat>
  gen_stages: Record<string, LatencyBucket>
  api_latency: Record<string, LatencyBucket>
  api_calls: Record<string, ApiCallStat>
  orchestration: OrchestrationStat
  frontend_perf: Record<string, LatencyBucket>
  frontend_access: Record<string, number>
  frontend_clicks: Record<string, number>
  generation_rate: { total: number; done: number; rate: number }
  error_stats?: Record<string, number>
  model_stats?: Record<string, { total: number; ok: number; fail: number; rate: number }>
  user_stats?: { dau_today: number; active_users: number; total_generations: number; avg_per_user: number }
  intent_decisions?: {
    by_decision: Record<string, number>
    by_skill: Record<string, number>
    by_risk: Record<string, number>
  }
  qc?: {
    count: number
    overall_avg: number | null
    review_rate: number
    per_dim_avg: Record<string, number>
    safety_dist: Record<string, number>
  }
  feedback?: {
    count: number
    avg_rating: number | null
    with_dims_rate: number
  }
  error?: string
}
const al = ref<AnalyticsSnapshot | null>(null)
const alLoading = ref(false)
let alTimer: ReturnType<typeof setInterval> | null = null

async function fetchAnalytics() {
  alLoading.value = true
  try {
    al.value = await get('/admin/analytics')
  } catch { /* ignore */ }
  finally { alLoading.value = false }
}

const ERROR_LABELS: Record<string, string> = {
  rate_limited: '配额限流', model_unavailable: '模型不可用', upstream_error: '上游故障', timeout: '超时', unknown: '未分类',
}

const PERF_LABELS: Record<string, string> = {
  page_load: '全页加载', ttfb: '首字节(TTFB)', dom_ready: 'DOM 就绪',
}
const STAGE_LABELS_ANA: Record<string, string> = {
  enter_planner: 'Planner', enter_coder: 'Coder', enter_reviewer: 'Reviewer', previewing: '预览投递',
}
function fmtMs(v: number): string { return Math.round(v) + 'ms' }

function statusLabel(s: string) {
  const m: Record<string, string> = { running: '生成中', done: '完成', error: '错误', aborted: '已取消' }
  return m[s] || s
}

function strategyLabel(s: string) {
  const m: Record<string, string> = { parallel: '全并行', mixed: '分层串行' }
  return m[s] || s
}

function riskLabel(s: string) {
  const m: Record<string, string> = { high: '高', medium: '中', low: '低' }
  return m[s] || s
}

function eventTypeLabel(t: string) {
  const m: Record<string, string> = { node: '节点', think: '思考', plan: '计划', token: '输出', error: '错误', done: '完成', aborted: '取消', degraded: '降级' }
  return m[t] || t
}
function decisionLabel(d: string) {
  const m: Record<string, string> = {
    block: '安全拦截', confirm: '二次确认', options: '多选项',
    route: '直接路由', fallback: '降级兜底', unsupported: '不支持',
  }
  return m[d] || d
}

onMounted(() => {
  connectMetrics()
  if (isSuper.value) fetchUsers()
  fetchQuality()
  fetchTraces()
})
watch(activeTab, (t) => {
  if (t === 'analytics') {
    if (!al.value) fetchAnalytics()
    if (!alTimer) alTimer = setInterval(fetchAnalytics, 15000)
  } else {
    if (alTimer) { clearInterval(alTimer); alTimer = null }
  }
})
onUnmounted(() => {
  es?.close()
})
</script>

<template>
  <div class="admin">
    <header class="head">
      <h1>管理后台</h1>
      <span class="role">当前身份:{{ currentRoleLabel }}</span>
    </header>

    <nav class="tabs">
      <button
        v-for="t in visibleTabs"
        :key="t.key"
        :class="{ on: activeTab === t.key }"
        @click="activeTab = t.key"
      >
        {{ t.label }}
      </button>
    </nav>

    <!-- 运行指标 -->
    <section v-if="activeTab === 'metrics'" class="panel">
      <div class="cards">
        <div class="card">
          <div class="k">运行时长</div>
          <div class="v">{{ fmtUptime(metrics.uptime_s) }}</div>
        </div>
        <div class="card">
          <div class="k">累计请求</div>
          <div class="v">{{ metrics.requests_total ?? '-' }}</div>
        </div>
        <div class="card">
          <div class="k">错误请求</div>
          <div class="v err">{{ metrics.requests_error ?? '-' }}</div>
        </div>
        <div class="card">
          <div class="k">每分钟请求</div>
          <div class="v">{{ metrics.requests_per_min ?? '-' }}</div>
        </div>
      </div>

      <!-- 数据库状态 -->
      <div v-if="dbItems.length" class="block">
        <h3>数据库状态</h3>
        <div class="db-grid">
          <div v-for="item in dbItems" :key="item.key" class="db-card">
            <span class="db-icon" :class="item.ok ? 'ok' : 'err'">{{ item.ok ? '●' : '●' }}</span>
            <span class="db-name">{{ item.key }}</span>
            <span class="db-stat" :class="item.ok ? 'ok' : 'err'">{{ item.ok ? '正常' : (item.error || '不可达') }}</span>
            <span v-if="item.ok && item.pool_size != null" class="db-pool">
              连接池: {{ item.pool_size }} (在用 {{ item.checked_in ?? '-' }}, 溢出 {{ item.overflow ?? '-' }})
            </span>
          </div>
        </div>
      </div>

      <div class="block">
        <h3>模型用量（次数 / Token / 估算花费）</h3>
        <div v-if="!metrics.model_usage || Object.keys(metrics.model_usage).length === 0" class="muted">暂无数据</div>
        <table v-else class="model-table">
          <thead><tr><th>模型</th><th>请求次数</th><th>Token 消耗</th><th>估算花费(USD)</th></tr></thead>
          <tbody>
            <tr v-for="(info, model) in metrics.model_usage" :key="model">
              <td class="mname">{{ model }}</td>
              <td>{{ info.count || info.raw_count || 0 }}</td>
              <td>{{ (info.tokens || 0).toLocaleString() }}</td>
              <td>${{ (info.est_cost || 0).toFixed(4) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="block" v-if="metrics.api_latency && Object.keys(metrics.api_latency).length">
        <h3>API 接口延迟 (ms)</h3>
        <table class="model-table">
          <thead><tr><th>接口</th><th>P50</th><th>P90</th><th>P99</th><th>平均</th><th>样本</th></tr></thead>
          <tbody>
            <tr v-for="(lat, path) in metrics.api_latency" :key="path">
              <td><code>{{ path }}</code></td>
              <td>{{ lat.p50 }}</td><td>{{ lat.p90 }}</td><td>{{ lat.p99 }}</td>
              <td>{{ lat.avg }}</td><td>{{ lat.samples }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- AI 质量(③-a) -->
    <section v-else-if="activeTab === 'quality'" class="panel">
      <div class="bar">
        <h3>AI 生成质量</h3>
        <button class="refresh" :disabled="qualityLoading" @click="fetchQuality">刷新</button>
      </div>
      <div v-if="quality" class="cards" style="grid-template-columns: repeat(4, 1fr);">
        <div class="card">
          <div class="k">平均评分</div>
          <div class="v">{{ quality.avg_rating ?? '-' }}</div>
        </div>
        <div class="card">
          <div class="k">评价数</div>
          <div class="v">{{ quality.feedback_count }}</div>
        </div>
        <div class="card">
          <div class="k">评审通过率</div>
          <div class="v">{{ (quality.reviewer_pass_rate * 100).toFixed(0) }}%</div>
        </div>
        <div class="card">
          <div class="k">生成成功率</div>
          <div class="v">{{ (quality.generation_success_rate * 100).toFixed(0) }}%</div>
        </div>
        <div class="card">
          <div class="k">不支持意图</div>
          <div class="v">{{ quality.unsupported_count ?? 0 }}</div>
        </div>
        <div class="card">
          <div class="k">QC 样本数</div>
          <div class="v">{{ quality.qc_count ?? 0 }}</div>
        </div>
        <div class="card">
          <div class="k">QC 整体均分</div>
          <div class="v">{{ quality.qc_overall_avg != null ? quality.qc_overall_avg.toFixed(2) : '-' }}</div>
        </div>
        <div class="card">
          <div class="k">需复核占比</div>
          <div class="v">{{ ((quality.qc_review_rate ?? 0) * 100).toFixed(0) }}%</div>
        </div>
      </div>
      <!-- QC 六维雷达图(v0.8.5 M1) -->
      <div v-if="qcSeries.length" class="block qc-radar">
        <h3>QC 六维雷达(三裁判 + 整体)</h3>
        <RadarChart
          :axes="(quality?.qc_dimensions || []).map((d: string) => quality?.qc_dim_labels?.[d] || d)"
          :series="qcSeries"
          :size="340"
        />
      </div>
      <div v-if="quality && quality.rating_distribution && Object.keys(quality.rating_distribution).length" class="block">
        <h3>评分分布</h3>
        <div class="dist">
          <template v-for="n in 10" :key="n">
            <span class="dn">{{ n }}</span>
            <span class="dbar"><span class="dfill" :style="{ width: (quality.feedback_count ? ((quality.rating_distribution[n] || 0) / quality.feedback_count * 100) : 0) + '%' }"></span></span>
            <span class="dcnt">{{ quality.rating_distribution[n] || 0 }}</span>
          </template>
        </div>
      </div>
      <div v-if="quality && quality.model_usage && Object.keys(quality.model_usage).length" class="block">
        <h3>模型用量(生成次数)</h3>
        <ul class="usage">
          <li v-for="(cnt, model) in quality.model_usage" :key="model">
            <span class="mname">{{ model }}</span>
            <span class="mcnt">{{ cnt }}</span>
          </li>
        </ul>
      </div>
      <div v-if="!quality && !qualityLoading" class="muted">暂无质量数据</div>
    </section>

    <!-- 回放(③-a) -->
    <section v-else-if="activeTab === 'replay'" class="panel">
      <div class="bar">
        <h3>生成回放</h3>
        <button class="refresh" :disabled="tracesLoading" @click="fetchTraces">刷新</button>
      </div>
      <div v-if="selectedTrace" class="block">
        <button class="back" @click="selectedTrace = null">← 返回列表</button>
        <p class="hint">Trace: {{ selectedTrace.trace.trace_id }} | 模型: {{ selectedTrace.trace.model_id || '-' }} | 状态: {{ statusLabel(selectedTrace.trace.status) }} | Token: ~{{ selectedTrace.trace.total_tokens }}</p>

        <!-- 后置 QC 三裁判详情 -->
        <div v-if="selectedTrace.qc && selectedTrace.qc.result?.dimensions" class="block">
          <h3>
            后置 QC 三裁判
            <span class="pill">整体 {{ selectedTrace.qc.overall.toFixed(2) }}</span>
            <span v-if="selectedTrace.qc.needs_review" class="pill warn">需复核</span>
            <span v-if="selectedTrace.qc.partial" class="pill gray">部分裁判</span>
            <span v-if="selectedTrace.qc.safety_risk && selectedTrace.qc.safety_risk !== 'low'" class="pill danger">{{ selectedTrace.qc.safety_risk }}</span>
          </h3>
          <table class="qctable">
            <thead><tr><th>维度</th><th>均值</th><th>DeepSeek</th><th>Qwen</th><th>HY3</th></tr></thead>
            <tbody>
              <tr v-for="d in Object.keys(selectedTrace.qc.result.dimensions)" :key="d">
                <td>{{ qcLabel(d) }}</td>
                <td>{{ selectedTrace.qc.result.dimensions[d].mean.toFixed(1) }}</td>
                <td>{{ selectedTrace.qc.result.dimensions[d].scores?.[0] || '-' }}</td>
                <td>{{ selectedTrace.qc.result.dimensions[d].scores?.[1] || '-' }}</td>
                <td>{{ selectedTrace.qc.result.dimensions[d].scores?.[2] || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 用户反馈 -->
        <div v-if="selectedTrace.feedback" class="block">
          <h3>用户评价</h3>
          <div class="fb-row">评分: <b>{{ selectedTrace.feedback.rating }}</b> / 10</div>
          <div v-if="selectedTrace.feedback.comment" class="fb-row">评语: {{ selectedTrace.feedback.comment }}</div>
          <div v-if="selectedTrace.feedback.dimensions" class="fb-dims">
            <span v-for="(v, k) in selectedTrace.feedback.dimensions" :key="k" class="fb-dim">{{ qcLabel(String(k)) }}: {{ v }}</span>
          </div>
          <div v-else class="muted">（仅整体评分，无多维细分）</div>
        </div>

        <!-- 对话内容 -->
        <div v-if="selectedTrace.messages && selectedTrace.messages.length" class="block">
          <h3>对话内容</h3>
          <div v-for="(m, i) in selectedTrace.messages" :key="i" class="msg" :class="m.role">
            <div class="msg-role">{{ m.role === 'user' ? '用户' : 'AI' }}</div>
            <div class="msg-body">{{ m.content }}</div>
          </div>
        </div>

        <div v-if="selectedTrace.events.length" class="events">
          <div v-for="(e, i) in selectedTrace.events" :key="i" class="evt">
            <span class="eseq">{{ e.seq }}</span>
            <span class="etype">{{ eventTypeLabel(e.event_type) }}</span>
            <span v-if="e.stage" class="estage">{{ e.stage }}</span>
            <span v-if="e.payload && typeof e.payload === 'object' && (e.payload as any).comment" class="ecomment">{{ (e.payload as any).comment }}</span>
          </div>
        </div>
        <p v-else class="muted">该 Trace 没有结构化事件</p>
      </div>
      <table v-else class="utable">
        <thead>
          <tr><th>Trace ID</th><th>用户输入</th><th>模型</th><th>状态</th><th>QC</th><th>评分</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="t in traces" :key="t.id" style="cursor:pointer;" @click="viewTrace(t.trace_id)">
            <td>{{ t.trace_id.slice(0, 12) }}</td>
            <td class="user-input">{{ (t as any).user_input || '-' }}</td>
            <td>{{ t.model_id || '-' }}</td>
            <td>{{ statusLabel(t.status) }}</td>
            <td>{{ t.qc_overall != null ? t.qc_overall.toFixed(1) : '-' }}</td>
            <td>{{ t.feedback_rating != null ? t.feedback_rating : '-' }}</td>
            <td>{{ t.started_at?.slice(0, 19) || '-' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!traces.length && !tracesLoading" class="muted">暂无生成记录</p>
    </section>

    <!-- 系统分析 -->
    <section v-else-if="activeTab === 'analytics'" class="panel">
      <div class="bar"><h3>系统分析</h3><button class="refresh" :disabled="alLoading" @click="fetchAnalytics">刷新</button></div>
      <div v-if="al?.error" class="muted">加载失败: {{ al.error }}</div>
      <template v-else-if="al">
        <!-- 意图命中率 -->
        <div class="block">
          <h4>意图命中率</h4>
          <table v-if="al.intent_stats && Object.keys(al.intent_stats).length" class="atable">
            <thead><tr><th>意图</th><th>命中</th><th>总数</th><th>命中率</th><th>指示</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.intent_stats" :key="k">
                <td>{{ k }}</td><td>{{ v.ok }}</td><td>{{ v.total }}</td>
                <td>{{ (v.rate * 100).toFixed(0) }}%</td>
                <td><span class="dot" :style="{ background: v.rate > 0.7 ? '#22c55e' : v.rate > 0.3 ? '#f59e0b' : '#ef4444' }"></span></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- 意图决策分布 -->
        <div class="block" v-if="al.intent_decisions && al.intent_decisions.by_decision && Object.keys(al.intent_decisions.by_decision).length">
          <h4>意图决策分布</h4>
          <table class="atable">
            <thead><tr><th>决策</th><th>次数</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.intent_decisions.by_decision" :key="k">
                <td>{{ decisionLabel(k) }}</td><td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="al.intent_decisions.by_risk && Object.keys(al.intent_decisions.by_risk).length" class="muted">
            高风险拦截: {{ al.intent_decisions.by_risk.high || 0 }} · 致命拦截: {{ al.intent_decisions.by_risk.critical || 0 }}
          </p>
        </div>
        <!-- Skill 成功率 -->
        <div class="block">
          <h4>Skill 成效</h4>
          <table v-if="al.skill_outcomes && Object.keys(al.skill_outcomes).length" class="atable">
            <thead><tr><th>技能</th><th>成功</th><th>失败</th><th>中断</th><th>成功率</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.skill_outcomes" :key="k">
                <td>{{ k }}</td><td>{{ v.ok }}</td><td>{{ v.fail }}</td><td>{{ v.abort }}</td>
                <td>{{ (v.success_rate * 100).toFixed(0) }}%</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- 生成阶段耗时 -->
        <div class="block">
          <h4>生成阶段耗时</h4>
          <table v-if="al.gen_stages && Object.keys(al.gen_stages).length" class="atable">
            <thead><tr><th>阶段</th><th>P50</th><th>P90</th><th>P99</th><th>均值</th><th>样本</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.gen_stages" :key="k">
                <td>{{ STAGE_LABELS_ANA[k] || k }}</td>
                <td>{{ fmtMs(v.p50) }}</td><td>{{ fmtMs(v.p90) }}</td><td>{{ fmtMs(v.p99) }}</td>
                <td>{{ fmtMs(v.avg) }}</td><td>{{ v.samples }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- API 延迟 -->
        <div class="block">
          <h4>API 响应时间</h4>
          <table v-if="al.api_latency && Object.keys(al.api_latency).length" class="atable">
            <thead><tr><th>端点</th><th>P50</th><th>P90</th><th>P99</th><th>均值</th><th>样本</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.api_latency" :key="k">
                <td>{{ k }}</td>
                <td>{{ fmtMs(v.p50) }}</td><td>{{ fmtMs(v.p90) }}</td><td>{{ fmtMs(v.p99) }}</td>
                <td>{{ fmtMs(v.avg) }}</td><td>{{ v.samples }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- 业务接口统计(STAT-2) -->
        <div class="block">
          <h4>业务接口调用</h4>
          <table v-if="al.api_calls && Object.keys(al.api_calls).length" class="atable">
            <thead><tr><th>端点</th><th>调用</th><th>成功</th><th>失败</th><th>成功率</th><th>P50</th><th>P90</th><th>P99</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.api_calls" :key="k">
                <td>{{ k }}</td><td>{{ v.total }}</td><td>{{ v.ok }}</td><td>{{ v.fail }}</td>
                <td>{{ (v.success_rate * 100).toFixed(0) }}%</td>
                <td>{{ fmtMs(v.latency.p50) }}</td><td>{{ fmtMs(v.latency.p90) }}</td><td>{{ fmtMs(v.latency.p99) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- AI 核心编排统计(STAT-1, 由 ai_service 写入同 Redis) -->
        <div class="block" v-if="al.orchestration && al.orchestration.total">
          <h4>AI 核心 · 多意图编排</h4>
          <div class="kv">
            <span>编排总次数</span><b>{{ al.orchestration.total }}</b>
            <span>平均子任务数</span><b>{{ al.orchestration.split_count ? al.orchestration.split_count.avg.toFixed(1) : '-' }}</b>
            <span>平均成功率</span><b>{{ al.orchestration.success_rate ? (al.orchestration.success_rate.avg * 100).toFixed(0) + '%' : '-' }}</b>
            <span>平均耗时</span><b>{{ al.orchestration.duration_ms ? fmtMs(al.orchestration.duration_ms.avg) : '-' }}</b>
          </div>
          <div v-if="al.orchestration.strategy_dist" class="muted" style="margin:6px 0;">
            策略分布:
            <span v-for="(v, k) in al.orchestration.strategy_dist" :key="k" class="pill">{{ strategyLabel(k) }} {{ v }}</span>
          </div>
          <div v-if="al.orchestration.sub_tasks && al.orchestration.sub_tasks.total">
            <h5>子任务状态</h5>
            <div class="muted">
              <span v-for="(v, k) in al.orchestration.sub_tasks.status_dist" :key="k" class="pill">{{ statusLabel(k) }} {{ v }}</span>
            </div>
            <h5>风险分布</h5>
            <div class="muted">
              <span v-for="(v, k) in al.orchestration.sub_tasks.risk_dist" :key="k" class="pill">{{ riskLabel(k) }} {{ v }}</span>
            </div>
            <h5>各 Skill 产出</h5>
            <table class="atable">
              <thead><tr><th>Skill</th><th>总数</th><th>完成</th><th>失败</th><th>拦截</th><th>跳过</th><th>成功率</th></tr></thead>
              <tbody>
                <tr v-for="(v, k) in al.orchestration.sub_tasks.per_skill" :key="k">
                  <td>{{ k }}</td><td>{{ v.total }}</td><td>{{ v.done }}</td><td>{{ v.failed }}</td><td>{{ v.blocked }}</td><td>{{ v.skipped }}</td>
                  <td>{{ (v.success_rate * 100).toFixed(0) }}%</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div v-else-if="al.orchestration && !al.orchestration.total" class="block">
          <h4>AI 核心 · 多意图编排</h4>
          <p class="muted">暂无多意图编排记录</p>
        </div>
        <!-- 前端性能 -->
        <div class="block">
          <h4>前端加载性能</h4>
          <table v-if="al.frontend_perf && Object.keys(al.frontend_perf).length" class="atable">
            <thead><tr><th>指标</th><th>P50</th><th>P90</th><th>P99</th><th>均值</th><th>样本</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.frontend_perf" :key="k">
                <td>{{ PERF_LABELS[k] || k }}</td>
                <td>{{ fmtMs(v.p50) }}</td><td>{{ fmtMs(v.p90) }}</td><td>{{ fmtMs(v.p99) }}</td>
                <td>{{ fmtMs(v.avg) }}</td><td>{{ v.samples }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- 前端访问统计(STAT-3) -->
        <div class="block">
          <h4>前端页面访问</h4>
          <table v-if="al.frontend_access && Object.keys(al.frontend_access).length" class="atable">
            <thead><tr><th>路由</th><th>访问次数</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.frontend_access" :key="k">
                <td>{{ k }}</td><td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- 前端点击统计(STAT-3) -->
        <div class="block">
          <h4>前端点击热点 (Top 20)</h4>
          <table v-if="al.frontend_clicks && Object.keys(al.frontend_clicks).length" class="atable">
            <thead><tr><th>元素</th><th>点击次数</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.frontend_clicks" :key="k">
                <td>{{ k }}</td><td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted">暂无数据</p>
        </div>
        <!-- 生成成功率 -->
        <div v-if="al.generation_rate" class="block">
          <h4>总体生成成功率</h4>
          <div class="rate-bar" :style="{ '--rate': al.generation_rate.rate * 100 + '%' }">
            <span>{{ (al.generation_rate.rate * 100).toFixed(1) }}%</span>
            <span class="rate-sub">({{ al.generation_rate.done }}/{{ al.generation_rate.total }})</span>
          </div>
        </div>
        <div v-if="al.error_stats && Object.keys(al.error_stats).length" class="block">
          <h4>错误分布</h4>
          <table class="atable"><thead><tr><th>类型</th><th>次数</th></tr></thead>
            <tbody><tr v-for="(v, k) in al.error_stats" :key="k"><td>{{ ERROR_LABELS[k] || k }}</td><td>{{ v }}</td></tr></tbody>
          </table>
        </div>
        <div v-if="al.model_stats && Object.keys(al.model_stats).length" class="block">
          <h4>模型分布</h4>
          <table class="atable"><thead><tr><th>模型</th><th>成功</th><th>失败</th><th>成功率</th></tr></thead>
            <tbody><tr v-for="(v, k) in al.model_stats" :key="k"><td>{{ k }}</td><td>{{ v.ok }}</td><td>{{ v.fail }}</td><td>{{ (v.rate * 100).toFixed(0) }}%</td></tr></tbody>
          </table>
        </div>
        <div v-if="al.user_stats" class="block">
          <h4>用户活跃</h4>
          <div class="card-row">
            <div class="card"><div class="k">今日DAU</div><div class="v">{{ al.user_stats.dau_today }}</div></div>
            <div class="card"><div class="k">活跃用户</div><div class="v">{{ al.user_stats.active_users }}</div></div>
            <div class="card"><div class="k">总生成</div><div class="v">{{ al.user_stats.total_generations }}</div></div>
            <div class="card"><div class="k">人均生成</div><div class="v">{{ al.user_stats.avg_per_user }}</div></div>
          </div>
        </div>
        <!-- 后置三裁判 QC(v0.8.5) -->
        <div v-if="al.qc && al.qc.count" class="block">
          <h4>后置三裁判质检 (QC)</h4>
          <div class="card-row">
            <div class="card"><div class="k">触发次数</div><div class="v">{{ al.qc.count }}</div></div>
            <div class="card"><div class="k">整体均分</div><div class="v">{{ al.qc.overall_avg ?? '-' }}</div></div>
            <div class="card"><div class="k">需复核率</div><div class="v">{{ (al.qc.review_rate * 100).toFixed(1) }}%</div></div>
          </div>
          <table class="atable">
            <thead><tr><th>维度</th><th>均分</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.qc.per_dim_avg" :key="k"><td>{{ qcLabel(k) }}</td><td>{{ v }}</td></tr>
            </tbody>
          </table>
          <p v-if="al.qc.safety_dist && Object.keys(al.qc.safety_dist).length" class="muted">
            安全风险分布: <span v-for="(v, k) in al.qc.safety_dist" :key="k">{{ k }} {{ v }} · </span>
          </p>
        </div>
        <!-- 用户评价(v0.8.5, 含六维子星) -->
        <div v-if="al.feedback && al.feedback.count" class="block">
          <h4>用户评价</h4>
          <div class="card-row">
            <div class="card"><div class="k">提交数</div><div class="v">{{ al.feedback.count }}</div></div>
            <div class="card"><div class="k">平均评分</div><div class="v">{{ al.feedback.avg_rating ?? '-' }}</div></div>
            <div class="card"><div class="k">含多维占比</div><div class="v">{{ (al.feedback.with_dims_rate * 100).toFixed(0) }}%</div></div>
          </div>
        </div>
      </template>
      <p v-if="!al && !alLoading" class="muted">点击刷新加载分析数据</p>
    </section>

    <!-- 用户管理(仅超管) -->
    <section v-else-if="activeTab === 'users' && isSuper" class="panel">
      <div class="bar">
        <h3>用户列表</h3>
        <button class="refresh" :disabled="usersLoading" @click="fetchUsers">刷新</button>
      </div>
      <table class="utable">
        <thead>
          <tr>
            <th>ID</th>
            <th>用户名</th>
            <th>昵称</th>
            <th>邮箱</th>
            <th>角色</th>
            <th>套餐</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.id }}</td>
            <td>{{ u.username }}</td>
            <td>{{ u.nickname || '-' }}</td>
            <td>{{ u.email || '-' }}</td>
            <td>
              <select
                :value="u.role"
                :disabled="roleDisabled(u)"
                @change="changeRole(u, ($event.target as HTMLSelectElement).value)"
              >
                <option value="user">普通用户</option>
                <option value="admin">管理员</option>
                <option value="super_admin">超级管理员</option>
              </select>
            </td>
            <td>
              <select
                :value="u.plan"
                @change="changePlan(u, ($event.target as HTMLSelectElement).value)"
              >
                <option value="free">free</option>
                <option value="pro">pro</option>
                <option value="team">team</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="hint">提示:超级管理员不可被降级,也不能取消自己的超管角色(后端强制)。</p>
    </section>

    <!-- 控制面(仅超管) -->
    <section v-else-if="activeTab === 'control' && isSuper" class="panel">
      <div class="block">
        <h3>扩缩容</h3>
        <div class="ctrl">
          <input v-model="scaleName" placeholder="服务名 (如 ai_service)" />
          <input v-model.number="scaleReplicas" type="number" min="1" max="10" />
          <button @click="doScale">提交</button>
        </div>
      </div>
      <div class="block">
        <h3>停止服务</h3>
        <div class="ctrl">
          <input v-model="stopName" placeholder="服务名 (如 ai_service)" />
          <button class="danger" @click="doStop">停止</button>
        </div>
      </div>
      <div class="block">
        <h3>🛡 重置系统</h3>
        <p class="hint">清空全部数据库 + Redis + 前端本地数据，重建表并创建默认超管。需重启服务。</p>
        <button class="danger" :disabled="resetLoading" @click="doReset">
          {{ resetLoading ? '执行中…' : '确认重置' }}
        </button>
        <pre v-if="resetMsg" class="reset-log">{{ resetMsg }}</pre>
      </div>
      <p v-if="ctrlMsg" class="ctrlmsg">{{ ctrlMsg }}</p>
      <p class="hint">控制面为占位实现(M1 接 DockerComposeOrchestrator / K8s),当前仅 ack。</p>
    </section>

    <section v-else class="panel">
      <p class="muted">无权限访问该模块。</p>
    </section>
  </div>
</template>

<style scoped>
.admin {
  flex: 1;
  padding: 20px 24px;
  overflow: auto;
}
.head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 14px;
}
.head h1 {
  font-size: 20px;
  margin: 0;
  color: var(--brand);
}
.role {
  font-size: 13px;
  color: var(--muted);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 10px;
}
.tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.tabs button {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 8px;
  padding: 6px 14px;
  cursor: pointer;
  font-size: 13px;
  color: var(--muted);
}
.tabs button.on {
  color: var(--brand);
  border-color: var(--brand2, #c7d2fe);
  background: #eef2ff;
  font-weight: 600;
}
.panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}
.card .k {
  font-size: 12px;
  color: var(--muted);
}
.card .v {
  font-size: 22px;
  font-weight: 700;
  margin-top: 6px;
  color: #1e293b;
}
.card-row { display: flex; gap: 12px; flex-wrap: wrap; }
.card-row .card { min-width: 120px; flex: 1; }
.card-row .v { font-size: 18px; }
.card .v.err {
  color: var(--err);
}
.block {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}
.block h3 {
  margin: 0 0 10px;
  font-size: 14px;
  color: #1e293b;
}
.block h4 {
  margin: 0 0 10px;
  font-size: 14px;
  color: var(--brand);
}
.block h5 {
  margin: 14px 0 6px;
  font-size: 12px;
  color: var(--muted);
  font-weight: 700;
}
.kv {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 14px;
  font-size: 13px;
  align-items: center;
}
.kv span { color: var(--muted); }
.kv b { color: #1e293b; font-weight: 700; }
.pill {
  display: inline-block;
  margin: 2px 6px 2px 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: #eef2ff;
  color: var(--brand);
  font-size: 12px;
  font-weight: 600;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
.usage {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.usage li {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.mname {
  width: 90px;
  color: #334155;
}
.mbar {
  flex: 1;
  height: 8px;
  background: var(--border);
  border-radius: 999px;
  overflow: hidden;
}
.mfill {
  display: block;
  height: 100%;
  background: var(--brand);
}
.mcnt {
  width: 40px;
  text-align: right;
  color: var(--muted);
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
}
.bar h3 {
  margin: 0;
}
.refresh {
  margin-left: auto;
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 8px;
  padding: 4px 12px;
  cursor: pointer;
  font-size: 13px;
}
.utable {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.utable th,
.utable td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}
.utable th {
  color: var(--muted);
  font-weight: 600;
}
.utable select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 6px;
  font-size: 13px;
}
.utable select:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.hint {
  font-size: 12px;
  color: var(--muted);
}
.ctrl {
  display: flex;
  gap: 10px;
  align-items: center;
}
.ctrl input {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
}
.ctrl button {
  border: 1px solid var(--brand);
  background: var(--brand);
  color: #fff;
  border-radius: 8px;
  padding: 6px 14px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
.ctrl button.danger {
  border-color: var(--err);
  background: var(--err);
}
.ctrlmsg {
  font-size: 13px;
  color: var(--brand);
}
.dist {
  display: grid;
  grid-template-columns: 24px 1fr 32px;
  gap: 4px 8px;
  align-items: center;
  font-size: 12px;
}
.dn { color: var(--muted); text-align: right; }
.dbar { height: 10px; background: var(--border); border-radius: 999px; overflow: hidden; }
.dfill { display: block; height: 100%; background: var(--brand); }
.dcnt { color: var(--muted); }
.events { max-height: 400px; overflow: auto; }
.evt { display: flex; gap: 10px; font-size: 13px; padding: 4px 0; border-bottom: 1px solid var(--border); }
.eseq { width: 28px; color: var(--muted); text-align: right; }
.etype { width: 48px; font-weight: 600; color: var(--brand); }
.estage { color: #64748b; }
.ecomment { color: var(--muted); font-style: italic; margin-left: auto; }
.back { border: 1px solid var(--border); background: var(--panel); border-radius: 8px; padding: 4px 12px; cursor: pointer; font-size: 13px; margin-bottom: 8px; }
.db-grid { display: flex; gap: 12px; flex-wrap: wrap; }
.db-card { display: flex; align-items: center; gap: 8px; background: #f8fafc; border-radius: 8px; padding: 10px 14px; min-width: 200px; }
.db-icon { font-size: 12px; }
.db-icon.ok { color: #22c55e; }
.db-icon.err { color: var(--err); }
.db-name { font-weight: 700; font-size: 14px; color: #334155; text-transform: uppercase; }
.db-stat { font-size: 12px; }
.db-stat.ok { color: #22c55e; }
.db-stat.err { color: var(--err); }
.db-pool { font-size: 11px; color: var(--muted); margin-left: auto; }

/* 系统分析表 */
.atable { width: 100%; border-collapse: collapse; font-size: 13px; }
.atable th { text-align: left; padding: 6px 8px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }
.atable td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.atable .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
.rate-bar { display: flex; align-items: center; gap: 12px; font-size: 22px; font-weight: 700; color: #1e293b; position: relative; padding: 10px 0; }
.rate-bar::before { content: ''; position: absolute; bottom: 0; left: 0; height: 4px; border-radius: 2px; background: linear-gradient(90deg, #22c55e var(--rate), #fee2e2 var(--rate)); width: 100%; }
.rate-sub { font-size: 13px; color: var(--muted); font-weight: 400; }
h4 { margin: 12px 0 8px; font-size: 14px; color: #1e293b; }
.reset-log { white-space: pre-wrap; font-size: 12px; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 10px 12px; margin-top: 10px; color: #991b1b; line-height: 1.6; }

/* QC 雷达 + 复盘详情(v0.8.5 M1) */
.qc-radar { display: flex; flex-direction: column; align-items: center; }
.qc-radar h3 { align-self: flex-start; }
.pill { display: inline-block; font-size: 11px; font-weight: 700; padding: 1px 8px; border-radius: 999px; background: #ede9fe; color: #6d28d9; margin-left: 6px; }
.pill.warn { background: #fef3c7; color: #b45309; }
.pill.danger { background: #fee2e2; color: #b91c1c; }
.pill.gray { background: #f1f5f9; color: #64748b; }
.qctable { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.qctable th { text-align: left; padding: 6px 8px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }
.qctable td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.fb-row { font-size: 13px; margin: 4px 0; }
.fb-dims { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.fb-dim { font-size: 12px; background: #f1f5f9; border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; color: #475569; }
.msg { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin: 6px 0; }
.msg.user { background: #eef2ff; }
.msg.assistant { background: #fff; }
.msg-role { font-size: 11px; color: var(--muted); font-weight: 600; margin-bottom: 2px; }
.msg-body { font-size: 13px; white-space: pre-wrap; word-break: break-word; max-height: 280px; overflow: auto; }
.model-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.model-table th { text-align: left; padding: 6px 10px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }
.model-table td { padding: 6px 10px; border-bottom: 1px solid var(--border); }
.model-table .mname { font-weight: 600; color: var(--primary, #2563eb); }
.user-input { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--muted); }
</style>
