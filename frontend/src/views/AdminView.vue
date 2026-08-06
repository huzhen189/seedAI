<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { get, post, delJson } from '../api/client'
import { useAuthStore } from '../stores/auth'
import RadarChart from '../components/RadarChart.vue'
import SystemRulesAdmin from '../components/SystemRulesAdmin.vue'
import { ROLE_LABELS, QC_DIM_LABELS, type AdminUser, type DbCapacity, type MetricsSnapshot, type Role } from '../types'

const auth = useAuthStore()
const isSuper = computed(() => auth.user?.role === 'super_admin')
const currentRoleLabel = computed(
  () => ROLE_LABELS[(auth.user?.role as Role) || 'user'] || auth.user?.role || '-',
)

// ---- 标签页(RBAC:用户管理 / 控制面 仅超管可见) ----
// v2.0.0 重组: 运行指标(服务器/三库/模型用量/API延迟) · AI质量(雷达/Skill/生成阶段/LLM) · 前端分析(UV/PV/性能/点击)
type Tab = 'metrics' | 'users' | 'control' | 'quality' | 'replay' | 'frontend' | 'vector' | 'sysrules'
const tabs: { key: Tab; label: string; superOnly: boolean }[] = [
  { key: 'metrics', label: '运行指标', superOnly: false },
  { key: 'quality', label: 'AI 质量', superOnly: false },
  { key: 'replay', label: '回放', superOnly: false },
  { key: 'frontend', label: '前端分析', superOnly: false },
  { key: 'vector', label: '向量库', superOnly: true },
  { key: 'users', label: '用户管理', superOnly: true },
  { key: 'control', label: '控制面', superOnly: true },
  // 系统规则：双轨(MySQL 原文 × 向量摘要)管理页，超管专用 CRUD + 统计，见 SystemRulesAdmin.vue。
  { key: 'sysrules', label: '系统规则', superOnly: true },
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

function fmtBytes(n?: number): string {
  if (n == null) return '-'
  if (n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(2)} ${units[i]}`
}

function fmtLoadAvg(l?: number[] | null): string {
  if (!l || l.length === 0) return '-'
  return l.map((x) => x.toFixed(2)).join(' / ')
}

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

async function changeTier(u: AdminUser, tier: string) {
  try {
    const updated = await post(`/admin/users/${u.id}/tier`, { tier })
    u.tier = updated.tier
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
      resetMsg.value = `✅ ${r.message}\n已 DROP ${r.tables_dropped} 张表, Redis ${r.redis_cleared ? '已清空' : '清理失败'}。\n请手动重启单进程后端(7101)，刷新本页面重新登录。`
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
interface DbItem {
  key: string
  ok: boolean
  error?: string
  capacity?: DbCapacity
  pool_size?: number
  checked_in?: number
  overflow?: number
  max_connections?: number
  threads_connected?: number
  used_memory_human?: string
  maxmemory_human?: string
  connected_clients?: number
  db_keys?: number
  collection_count?: number
  item_count?: number
  collections?: { name: string; count: number }[]
}
const dbItems = computed<DbItem[]>(() => {
  const db = metrics.value.db
  if (!db) return []
  return Object.entries(db).map(([key, info]) => ({ key, ...(info as any) }))
})

// ---- R1: API 延迟两个子表单(业务端 / 需求端) ----
type LatencyGroup = 'business' | 'ai_service'
const latencyTab = ref<LatencyGroup>('business')
const currentLatency = computed<Record<string, LatencyBucket>>(() => {
  const groups = metrics.value.api_latency
  if (!groups) return {}
  return groups[latencyTab.value] || {}
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
  // QC 单裁判聚合(v2.3.0)
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

// 单裁判下每个实际出现的 QC 模型配一条固定色序列(避免撞色)
const QC_MODEL_COLORS = ['#15c4a4', '#16a34a', '#d97706', '#15b8c4', '#db2777', '#e8734a']
function modelColor(idx: number): string {
  return QC_MODEL_COLORS[idx % QC_MODEL_COLORS.length]
}

// 当前 QC 报表里实际出现的模型序列(供明细表表头 / 单元格遍历)
const qcModels = computed(() =>
  (quality.value?.qc_judges || []).filter((m) => m && m !== 'unknown'),
)

// QC 雷达图序列: 实际出现的模型各一条 + 整体基线(各 6 维)
const qcSeries = computed(() => {
  const q = quality.value
  if (!q || !q.qc_dimensions?.length) return []
  const axes = q.qc_dimensions
  const models = qcModels.value
  const mk = (name: string, color: string, src: Record<string, number>) => ({
    name, color, values: axes.map((d) => Number(src?.[d] ?? 0)),
  })
  const series: Array<{ name: string; color: string; values: number[] }> = models.map(
    (m, i) => mk(m, modelColor(i), q.qc_model_avg?.[m] || {}),
  )
  series.push(mk('整体', '#e8734a', q.qc_overall_dim_avg || {}))
  return series
})

// QC 评分明细表(各实际模型 + 整体): 把"评分"以数字清晰呈现
interface QcTableRow {
  dim: string
  label: string
  scores: Record<string, number | null>
  overall: number | null
}
const qcTable = computed<QcTableRow[]>(() => {
  const q = quality.value
  if (!q || !q.qc_dimensions?.length) return []
  const models = qcModels.value
  return q.qc_dimensions.map((d) => {
    const scores: Record<string, number | null> = {}
    for (const m of models) scores[m] = q.qc_model_avg?.[m]?.[d] ?? null
    return {
      dim: d,
      label: (QC_DIM_LABELS as Record<string, string>)[d] || d,
      scores,
      overall: q.qc_overall_dim_avg?.[d] ?? null,
    }
  })
})

// 维度 key -> 中文标签(兼容任意字符串 key, 避免模板内严格索引报错)
function qcLabel(d: string): string {
  return (QC_DIM_LABELS as Record<string, string>)[d] || d
}

// LLM 调用类型 purpose -> 中文标签(语义分析 / 结果总结 / 实际任务 等)
function llmPurposeLabel(p: string): string {
  const m: Record<string, string> = {
    intent: '语义分析', extract: '记忆/QC 提取', reply: '实际任务回复', health: '探活', other: '其他',
  }
  return m[p] || p
}

// ---- 回放(③-a) ----
interface TraceItem {
  id: number; trace_id: string; turn_id: string; user_id: number; model_id: string | null
  status: string; total_tokens: number; started_at: string | null; finished_at: string | null
  conversation_id: number | null; project_id: number | null
  qc_overall?: number | null
  feedback_rating?: number | null
}
interface TraceEventItem {
  seq: number; event_type: string; stage: string | null
  payload: unknown; created_at: string | null
}
interface QcJudgeDetail { model: string; valid: boolean; comment: string }
interface QcDetail {
  overall: number
  // 新单裁判 schema: result.scores = {维度: 0-100 整数}, result.rationale = 评语。
  // dimensions 为旧多裁判 schema 的遗留字段, 保留可选以兼容历史 trace 数据。
  result: {
    scores?: Record<string, number>
    rationale?: string
    dimensions?: Record<string, { mean: number; variance: number; scores: number[] }>
  }
  judges?: QcJudgeDetail[]
  needs_review: boolean
  safety_risk: string
  partial: boolean
  created_at: string | null
}
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
// 回放按 id 搜索(用户/项目/会话/trace·turn)
const traceFilters = ref<{ user_id: string; project_id: string; conversation_id: string; trace_id: string }>({
  user_id: '', project_id: '', conversation_id: '', trace_id: '',
})

async function fetchTraces() {
  tracesLoading.value = true
  try {
    const params = new URLSearchParams()
    params.set('limit', '50')
    if (traceFilters.value.user_id.trim()) params.set('user_id', traceFilters.value.user_id.trim())
    if (traceFilters.value.project_id.trim()) params.set('project_id', traceFilters.value.project_id.trim())
    if (traceFilters.value.conversation_id.trim()) params.set('conversation_id', traceFilters.value.conversation_id.trim())
    if (traceFilters.value.trace_id.trim()) params.set('trace_id', traceFilters.value.trace_id.trim())
    traces.value = await get(`/admin/traces?${params.toString()}`)
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
interface AiCoreIntent { total: number; decision_dist: Record<string, number>; source_dist: Record<string, number>; success_dist: Record<string, number>; confidence: LatencyBucket; duration_ms: LatencyBucket }
interface AiCoreQc { total: number; overall: LatencyBucket; needs_review: number; needs_review_rate: number; partial: number; partial_rate: number; safety_dist: Record<string, number>; dimensions: Record<string, LatencyBucket> }
interface AiCoreReviewerSkill { passed: number; failed: number; total: number; pass_rate: number }
interface AiCoreReviewer { total: number; per_skill: Record<string, AiCoreReviewerSkill>; needs_review: number; needs_review_rate: number; reason_dist: Record<string, number>; dimensions: Record<string, LatencyBucket> }
interface AiCoreSafety { total: number; risk_dist: Record<string, number>; outcome_dist: Record<string, number>; reason_dist: Record<string, number> }
interface AiCoreLlmModel { total: number; ok: number; fail: number; success_rate: number; err_dist: Record<string, number>; duration_ms: LatencyBucket; tokens_in: number; tokens_out: number }
interface AiCoreLlmPurpose { total: number; ok: number; fail: number; success_rate: number; tokens_in: number; tokens_out: number }
interface AiCoreLlm { total: number; models: Record<string, AiCoreLlmModel>; purposes?: Record<string, AiCoreLlmPurpose> }
interface AiCoreMultiIntent {
  total: number
  path_dist: Record<string, number>
  ab_ratio: { hybrid: number; llm: number }
  escalated: number
  escalate_rate: number
  sub_task_count: LatencyBucket
  duration_ms: LatencyBucket
}
interface AiCore { intent?: AiCoreIntent; qc?: AiCoreQc; reviewer?: AiCoreReviewer; safety?: AiCoreSafety; llm?: AiCoreLlm; multi_intent?: AiCoreMultiIntent }
interface AnalyticsSnapshot {
  ai_core?: AiCore
  intent_stats: Record<string, IntentStat>
  skill_outcomes: Record<string, SkillStat>
  gen_stages: Record<string, LatencyBucket>
  api_latency: Record<string, LatencyBucket>
  api_calls: Record<string, ApiCallStat>
  orchestration: OrchestrationStat
  frontend_perf: Record<string, LatencyBucket>
  frontend_access: Record<string, number>
  frontend_clicks: Record<string, number>
  frontend_uv?: { total: number; today: number }
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
  // v2.0.0 AI 质量 8 维雷达(0-100): 意图识别/LLM/Skill/生成/业务API/反馈/前端/编排
  radar?: {
    intent: number
    llm: number
    skill: number
    generation: number
    api: number
    feedback: number
    frontend: number
    orchestration: number
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


const PERF_LABELS: Record<string, string> = {
  page_load: '全页加载', ttfb: '首字节(TTFB)', dom_ready: 'DOM 就绪',
}
function fmtMs(v: number | null | undefined): string {
  if (v == null || isNaN(Number(v))) return '-'
  const ms = Number(v)
  // 普通人友好: 毫秒 -> 秒/分/时, 不裸显 ms
  if (ms < 1000) return `${Math.round(ms)} 毫秒`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} 秒`
  const m = Math.floor(s / 60)
  const rs = Math.round(s % 60)
  if (m < 60) return `${m} 分 ${rs} 秒`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return `${h} 小时 ${rm} 分`
}

function statusLabel(s: string) {
  const m: Record<string, string> = { running: '生成中', done: '完成', error: '错误', aborted: '已取消' }
  return m[s] || s
}



function eventTypeLabel(t: string) {
  const m: Record<string, string> = { node: '节点', think: '思考', plan: '计划', token: '输出', error: '错误', done: '完成', aborted: '取消', degraded: '降级' }
  return m[t] || t
}

// 回放链路：事件详情折叠态(seq -> 是否展开)
const expandedEvents = ref<Record<number, boolean>>({})
function toggleEvent(i: number) {
  expandedEvents.value = { ...expandedEvents.value, [i]: !expandedEvents.value[i] }
}
function expandAllEvents() {
  const next: Record<number, boolean> = {}
  selectedTrace.value?.events.forEach((_: unknown, i: number) => { next[i] = true })
  expandedEvents.value = next
}
function isObj(v: unknown): v is Record<string, any> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}
function pretty(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}
// 阶段事件 payload 摘要行(状态 / 耗时 / 变更字段数)
function evtStatus(p: unknown): string | null {
  return isObj(p) && typeof p.status === 'string' ? p.status : null
}
function evtDuration(p: unknown): number | null {
  return isObj(p) && typeof p.duration_ms === 'number' ? p.duration_ms : null
}
function evtChangedCount(p: unknown): number {
  return isObj(p) && Array.isArray(p.changed) ? p.changed.length : 0
}


onMounted(() => {
  connectMetrics()
  if (isSuper.value) fetchUsers()
  fetchQuality()
  fetchTraces()
  // 运行指标页需要 api_calls(访问量/失败/成功率/平均时长一行核心指标), 它来自 /admin/analytics;
  // 此处预拉一次, 后续由前端分析 tab 的 15s 轮询续接。
  fetchAnalytics()
})
onUnmounted(() => {
  es?.close()
})

// ---- 向量库可视化管理（超管专用） ----

// ---- 向量库可视化管理（超管专用） ----
interface VCollection {
  name: string
  count: number
  metadata: Record<string, any>
}
interface VPoint {
  id: string
  document: string
  metadata: Record<string, any>
}
interface VHit {
  id: string
  text: string
  metadata: Record<string, any>
  distance: number
}

const vectorCollections = ref<VCollection[]>([])
const vectorLoading = ref(false)
const vectorError = ref('')
const selectedCollection = ref('')
const browsePoints = ref<VPoint[]>([])
const browseWhere = ref('')
const browseLimit = ref(50)
const browseOffset = ref(0)
const browseHasMore = ref(false)
const browseLoading = ref(false)
const queryText = ref('')
const queryTopK = ref(10)
const queryHits = ref<VHit[]>([])
const queryLoading = ref(false)
const selectedPoint = ref<(VPoint & { embedding?: number[] }) | null>(null)
const pointLoading = ref(false)
const selectedIds = ref<Set<string>>(new Set())
const showAddPanel = ref(false)
const addDocument = ref('')
const addMetaRaw = ref('{}')
const addError = ref('')

async function fetchVectorCollections(): Promise<void> {
  vectorLoading.value = true
  vectorError.value = ''
  try {
    const data = await get('/admin/vector/collections')
    vectorCollections.value = (data.collections || []) as VCollection[]
  } catch (e) {
    vectorError.value = (e as Error).message || '加载集合失败'
    vectorCollections.value = []
  } finally {
    vectorLoading.value = false
  }
}

async function selectCollection(name: string): Promise<void> {
  selectedCollection.value = name
  browseOffset.value = 0
  selectedIds.value = new Set()
  await browseCollection()
  queryHits.value = []
}

async function browseCollection(): Promise<void> {
  if (!selectedCollection.value) return
  browseLoading.value = true
  vectorError.value = ''
  try {
    const params = new URLSearchParams({
      limit: String(browseLimit.value),
      offset: String(browseOffset.value),
    })
    if (browseWhere.value.trim()) params.set('where', browseWhere.value.trim())
    const data = await get(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}?${params.toString()}`,
    )
    browsePoints.value = (data.points || []) as VPoint[]
    browseHasMore.value = (data.points || []).length >= browseLimit.value
  } catch (e) {
    vectorError.value = (e as Error).message || '浏览失败'
    browsePoints.value = []
  } finally {
    browseLoading.value = false
  }
}

function browsePrev(): void {
  if (browseOffset.value >= browseLimit.value) {
    browseOffset.value -= browseLimit.value
    browseCollection()
  }
}
function browseNext(): void {
  if (browseHasMore.value) {
    browseOffset.value += browseLimit.value
    browseCollection()
  }
}

async function runQuery(): Promise<void> {
  if (!selectedCollection.value || !queryText.value.trim()) return
  queryLoading.value = true
  vectorError.value = ''
  try {
    const body: Record<string, unknown> = {
      query: queryText.value.trim(),
      top_k: queryTopK.value,
    }
    if (browseWhere.value.trim()) {
      try {
        body.where = JSON.parse(browseWhere.value.trim())
      } catch {
        vectorError.value = 'where 不是合法 JSON，已忽略检索过滤'
      }
    }
    const data = await post(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}/query`,
      body,
    )
    queryHits.value = (data.hits || []) as VHit[]
  } catch (e) {
    vectorError.value = (e as Error).message || '检索失败'
    queryHits.value = []
  } finally {
    queryLoading.value = false
  }
}

async function openPointDetail(id: string, withEmbedding = false): Promise<void> {
  if (!selectedCollection.value) return
  pointLoading.value = true
  try {
    const data = await get(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}/${encodeURIComponent(id)}?with_embedding=${withEmbedding}`,
    )
    selectedPoint.value = data.point as VPoint & { embedding?: number[] }
  } catch (e) {
    vectorError.value = (e as Error).message || '加载详情失败'
    selectedPoint.value = null
  } finally {
    pointLoading.value = false
  }
}

function toggleSelect(id: string): void {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}

async function deletePoints(ids: string[]): Promise<void> {
  if (!selectedCollection.value || ids.length === 0) return
  if (!confirm(`确认删除 ${ids.length} 个向量点？此操作不可撤销。`)) return
  vectorError.value = ''
  try {
    const res = await delJson(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}/points`,
      { ids },
    )
    await browseCollection()
    selectedIds.value = new Set()
    await fetchVectorCollections()
    vectorError.value = `已删除 ${res.deleted ?? ids.length} 个点`
  } catch (e) {
    vectorError.value = (e as Error).message || '删除失败'
  }
}

async function deleteByWhere(): Promise<void> {
  if (!selectedCollection.value) return
  if (!confirm('确认按 where 条件删除匹配的全部向量点？此操作不可撤销。')) return
  vectorError.value = ''
  let where: Record<string, any> | null = null
  if (browseWhere.value.trim()) {
    try {
      where = JSON.parse(browseWhere.value.trim())
    } catch {
      vectorError.value = 'where 不是合法 JSON'
      return
    }
  } else {
    vectorError.value = '请先填写 where 条件'
    return
  }
  try {
    const res = await delJson(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}/points`,
      { where },
    )
    await browseCollection()
    await fetchVectorCollections()
    vectorError.value = `已删除 ${res.deleted ?? 0} 个点`
  } catch (e) {
    vectorError.value = (e as Error).message || '删除失败'
  }
}

async function submitAdd(): Promise<void> {
  addError.value = ''
  if (!selectedCollection.value) return
  if (!addDocument.value.trim()) {
    addError.value = '文本不能为空'
    return
  }
  let metadata: Record<string, any> | undefined
  if (addMetaRaw.value.trim()) {
    try {
      metadata = JSON.parse(addMetaRaw.value.trim())
    } catch {
      addError.value = '元数据不是合法 JSON'
      return
    }
  }
  try {
    await post(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}/points`,
      { points: [{ document: addDocument.value.trim(), metadata }] },
    )
    showAddPanel.value = false
    addDocument.value = ''
    addMetaRaw.value = '{}'
    await browseCollection()
    await fetchVectorCollections()
    vectorError.value = '新增成功'
  } catch (e) {
    addError.value = (e as Error).message || '新增失败'
  }
}

async function clearCollection(): Promise<void> {
  if (!selectedCollection.value) return
  if (!confirm(`⚠️ 确认清空集合「${selectedCollection.value}」的所有向量点？集合本身保留，但数据全部删除不可撤销！`)) return
  vectorError.value = ''
  try {
    const res = await delJson(
      `/admin/vector/collections/${encodeURIComponent(selectedCollection.value)}/clear`,
      { confirm: true },
    )
    await browseCollection()
    await fetchVectorCollections()
    vectorError.value = `已清空 ${res.removed ?? 0} 个点`
  } catch (e) {
    vectorError.value = (e as Error).message || '清空失败'
  }
}

function refreshVector(): void {
  fetchVectorCollections()
  if (selectedCollection.value) browseCollection()
}

watch(activeTab, (t) => {
  if (t === 'vector') {
    fetchVectorCollections()
    if (selectedCollection.value) browseCollection()
  }
  if (t === 'frontend') {
    if (!al.value) fetchAnalytics()
    if (!alTimer) alTimer = setInterval(fetchAnalytics, 15000)
  } else {
    if (alTimer) {
      clearInterval(alTimer)
      alTimer = null
    }
  }
})

function fmtMeta(meta: Record<string, any> | undefined): string {
  if (!meta) return ''
  return Object.entries(meta)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(' · ')
}
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

      <!-- 数据库状态(R2: MySQL + Redis + Chroma 三库, 含容量/连接) -->
      <div v-if="dbItems.length" class="block">
        <h3>数据库状态（MySQL · Redis · Chroma 向量库）</h3>
        <div class="db-grid">
          <div v-for="item in dbItems" :key="item.key" class="db-card" :class="item.ok ? 'ok' : 'err'">
            <div class="db-head">
              <span class="db-name">{{ item.key.toUpperCase() }}</span>
              <span class="db-stat" :class="item.ok ? 'ok' : 'err'">{{ item.ok ? '正常' : (item.error || '不可达') }}</span>
            </div>
            <template v-if="item.ok">
              <div class="db-cap">
                <span class="db-cap-val">{{ item.capacity?.value }}</span>
                <span class="db-cap-pct" :class="{ none: item.capacity?.pct == null }">
                  {{ item.capacity?.pct != null ? item.capacity.pct + '%' : '—' }}
                </span>
              </div>
              <div class="db-cap-detail">{{ item.capacity?.detail }}</div>
              <div v-if="item.key === 'mysql'" class="db-extra">
                连接池 {{ item.pool_size }} · 在用 {{ item.checked_in ?? '-' }} · 溢出 {{ item.overflow ?? '-' }}
              </div>
              <div v-else-if="item.key === 'redis'" class="db-extra">
                内存 {{ item.used_memory_human }} · 客户端 {{ item.connected_clients }} · Keys {{ item.db_keys }}
              </div>
              <div v-else-if="item.key === 'chroma'" class="db-extra">
                {{ item.collection_count }} 个集合 · 共 {{ item.item_count }} 向量
              </div>
              <div v-if="item.key === 'chroma' && item.collections?.length" class="db-colls">
                <span v-for="c in item.collections" :key="c.name" class="pill">{{ c.name }}: {{ c.count }}</span>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- 服务器系统状态(主机 OS: 名称 / CPU / 内存 / 磁盘 / 开机时长) -->
      <div v-if="metrics.system" class="block">
        <h3>服务器系统状态</h3>
        <div class="sys-grid">
          <div class="sys-card">
            <div class="sys-k">操作系统</div>
            <div class="sys-v">{{ metrics.system.platform?.name || '-' }}</div>
            <div class="sys-sub">{{ metrics.system.kernel || metrics.system.arch || '' }}</div>
          </div>
          <div class="sys-card">
            <div class="sys-k">主机名</div>
            <div class="sys-v">{{ metrics.system.hostname || '-' }}</div>
            <div class="sys-sub">Python {{ metrics.system.python_version || '-' }}</div>
          </div>
          <div class="sys-card">
            <div class="sys-k">已开机</div>
            <div class="sys-v">{{ fmtUptime(metrics.system.boot_time ? (metrics.system.ts ?? 0) - metrics.system.boot_time : undefined) }}</div>
            <div class="sys-sub">运行时长 {{ fmtUptime(metrics.uptime_s) }}</div>
          </div>
          <div class="sys-card">
            <div class="sys-k">CPU 使用率</div>
            <div class="sys-v">
              <span :class="{ warn: (metrics.system.cpu_percent ?? 0) >= 85 }">
                {{ metrics.system.cpu_percent != null ? metrics.system.cpu_percent + '%' : '-' }}
              </span>
            </div>
            <div class="sys-sub">
              {{ metrics.system.cpu_cores ?? '-' }} 核 · 负载 {{ fmtLoadAvg(metrics.system.load_avg) }}
            </div>
          </div>
          <div class="sys-card">
            <div class="sys-k">内存使用率</div>
            <div class="sys-v">
              <span :class="{ warn: (metrics.system.mem?.percent ?? 0) >= 85 }">{{ metrics.system.mem?.percent != null ? metrics.system.mem.percent + '%' : '-' }}</span>
            </div>
            <div class="sys-sub" v-if="metrics.system.mem?.ok !== false">
              {{ fmtBytes(metrics.system.mem?.used) }} / {{ fmtBytes(metrics.system.mem?.total) }}
            </div>
            <div class="sys-sub err" v-else>{{ metrics.system.mem?.error || '读取失败' }}</div>
          </div>
          <div class="sys-card">
            <div class="sys-k">磁盘(总计)</div>
            <div class="sys-v">
              <span :class="{ warn: (metrics.system.disk?.percent ?? 0) >= 85 }">{{ metrics.system.disk?.percent != null ? metrics.system.disk.percent + '%' : '-' }}</span>
            </div>
            <div class="sys-sub" v-if="metrics.system.disk?.ok !== false">
              {{ fmtBytes(metrics.system.disk?.used) }} / {{ fmtBytes(metrics.system.disk?.total) }}
            </div>
            <div class="sys-sub err" v-else>{{ metrics.system.disk?.error || '读取失败' }}</div>
          </div>
        </div>

        <!-- 各分区明细 -->
        <div v-if="metrics.system.disk?.partitions?.length" class="sys-parts">
          <table class="model-table">
            <thead>
              <tr><th>挂载点</th><th>文件系统</th><th>已用 / 总量</th><th>可用</th><th>使用率</th></tr>
            </thead>
            <tbody>
              <tr v-for="p in metrics.system.disk.partitions" :key="p.mountpoint">
                <td><code>{{ p.mountpoint }}</code></td>
                <td>{{ p.fstype }}</td>
                <td>{{ fmtBytes(p.used) }} / {{ fmtBytes(p.total) }}</td>
                <td>{{ fmtBytes(p.free) }}</td>
                <td>
                  <span :class="{ warn: (p.percent ?? 0) >= 85 }">{{ p.percent != null ? p.percent + '%' : '-' }}</span>
                </td>
              </tr>
            </tbody>
          </table>
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
      <!-- 运行指标核心: API 一行核心指标(访问量 / 失败数 / 成功率 / 平均响应时长) -->
      <div class="block" v-if="al && al.api_calls && Object.keys(al.api_calls).length">
        <h3>API 接口核心指标（访问量 / 失败 / 成功率 / 平均时长）</h3>
        <table class="model-table">
          <thead><tr><th>接口</th><th>访问量</th><th>失败数</th><th>成功率</th><th>平均(ms)</th><th>P90(ms)</th><th>P99(ms)</th></tr></thead>
          <tbody>
            <tr v-for="(v, path) in al.api_calls" :key="path">
              <td><code>{{ path }}</code></td>
              <td>{{ v.total }}</td>
              <td class="err">{{ v.fail }}</td>
              <td>{{ (v.success_rate * 100).toFixed(1) }}%</td>
              <td>{{ fmtMs(v.latency.avg) }}</td>
              <td>{{ fmtMs(v.latency.p90) }}</td>
              <td>{{ fmtMs(v.latency.p99) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <!-- 旧: 仅分位延迟(无访问量/成功率), 已并入上方核心指标表; 保留 SSE 维度的原始延迟作为补充 -->
      <div class="block" v-else-if="metrics.api_latency">
        <h3>API 接口延迟 (ms)</h3>
        <table v-if="Object.keys(currentLatency).length" class="model-table">
          <thead><tr><th>接口</th><th>P50</th><th>P90</th><th>P99</th><th>平均</th><th>样本</th></tr></thead>
          <tbody>
            <tr v-for="(lat, path) in currentLatency" :key="path">
              <td><code>{{ path }}</code></td>
              <td>{{ lat.p50 }}</td><td>{{ lat.p90 }}</td><td>{{ lat.p99 }}</td>
              <td>{{ lat.avg }}</td><td>{{ lat.samples }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">该端点暂无延迟采样数据</p>
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
      <div v-if="(quality?.qc_count ?? 0) > 0 && qcSeries.length" class="block qc-radar">
        <h3>QC 六维雷达(实际模型 + 整体)</h3>
        <RadarChart
          :axes="(quality?.qc_dimensions || []).map((d: string) => quality?.qc_dim_labels?.[d] || d)"
          :series="qcSeries"
          :size="340"
        />
      </div>
      <!-- QC 评分明细(数字, R3) -->
      <div v-if="(quality?.qc_count ?? 0) > 0 && qcTable.length" class="block">
        <h3>QC 评分明细（模型 + 整体, 0-10）</h3>
        <table class="qctable">
          <thead>
            <tr>
              <th>维度</th>
              <th v-for="m in qcModels" :key="m">{{ m }}</th>
              <th>整体</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in qcTable" :key="r.dim">
              <td>{{ r.label }}</td>
              <td v-for="m in qcModels" :key="m">
                {{ r.scores[m] != null ? r.scores[m]!.toFixed(2) : '-' }}
              </td>
              <td><b>{{ r.overall != null ? r.overall.toFixed(2) : '-' }}</b></td>
            </tr>
          </tbody>
        </table>
      </div>
      <!-- 无 QC 评分数据时的清晰提示(避免全 0 雷达图让人误以为坏了) -->
      <div v-if="(quality?.qc_count ?? 0) === 0" class="block muted">
        <h3>QC 评分（雷达图 / 明细）</h3>
        <p>暂无 QC 评分数据。QC 单裁判仅对「需复核」或闲聊类生成 trace 触发评分，当前样本数 = 0。</p>
        <p class="hint">提示：在对话中发起一次网站生成，生成完成后系统会自动对该 trace 做后置 QC 评分，这里即可看到六维雷达图与评分明细。</p>
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

      <!-- ===== AI 质量 · 从 /admin/analytics 并入(Skill / 生成阶段 / AI核心 LLM+编排, 第6条) ===== -->
      <template v-if="al">
        <!-- AI 质量健康度雷达(8 维: 意图/LLM/Skill/生成/API/反馈/前端/编排) -->
        <div v-if="al.radar" class="block qc-radar">
          <h3>AI 质量健康度雷达（8 维，0-100）</h3>
          <RadarChart
            :axes="['意图识别','LLM调用','Skill成效','生成成功率','业务API','用户反馈','前端性能','编排成功率']"
            :series="[{
              name: '综合健康度',
              color: '#15c4a4',
              values: [
                al.radar.intent ?? 0, al.radar.llm ?? 0, al.radar.skill ?? 0, al.radar.generation ?? 0,
                al.radar.api ?? 0, al.radar.feedback ?? 0, al.radar.frontend ?? 0, al.radar.orchestration ?? 0,
              ],
            }]"
            :size="340"
          />
        </div>
        <!-- Skill 成效 -->
        <div class="block" v-if="al.skill_outcomes && Object.keys(al.skill_outcomes).length">
          <h4>Skill 调用成效</h4>
          <table class="atable">
            <thead><tr><th>技能</th><th>成功</th><th>失败</th><th>中断</th><th>成功率</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.skill_outcomes" :key="k">
                <td>{{ k }}</td><td>{{ v.ok }}</td><td>{{ v.fail }}</td><td>{{ v.abort }}</td>
                <td>{{ (v.success_rate * 100).toFixed(0) }}%</td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- Tools / 生成阶段耗时 -->
        <div class="block" v-if="al.gen_stages && Object.keys(al.gen_stages).length">
          <h4>生成阶段耗时（Tools 执行）</h4>
          <table class="atable">
            <thead><tr><th>阶段</th><th>P50</th><th>P90</th><th>P99</th><th>均值</th><th>样本</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.gen_stages" :key="k">
                <td>{{ k }}</td>
                <td>{{ fmtMs(v.p50) }}</td><td>{{ fmtMs(v.p90) }}</td><td>{{ fmtMs(v.p99) }}</td>
                <td>{{ fmtMs(v.avg) }}</td><td>{{ v.samples }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- AI 核心 · LLM Provider 调用 -->
        <div class="block" v-if="al.ai_core && al.ai_core.llm && al.ai_core.llm.total">
          <h4>LLM Provider 调用（模型用量 + 成功率）</h4>
          <table class="atable">
            <thead><tr><th>模型</th><th>次数</th><th>成功</th><th>失败</th><th>成功率</th><th>Token(in/out)</th><th>平均耗时</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.ai_core.llm.models" :key="k">
                <td>{{ k }}</td><td>{{ v.total }}</td><td>{{ v.ok }}</td><td>{{ v.fail }}</td>
                <td>{{ (v.success_rate * 100).toFixed(0) }}%</td>
                <td>{{ (v.tokens_in || 0).toLocaleString() }} / {{ (v.tokens_out || 0).toLocaleString() }}</td>
                <td>{{ fmtMs(v.duration_ms?.avg ?? 0) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- AI 核心 · LLM 调用类型分布(语义分析 / 结果总结 / 实际任务) -->
        <div class="block" v-if="al.ai_core && al.ai_core.llm && al.ai_core.llm.purposes && Object.keys(al.ai_core.llm.purposes).length">
          <h4>LLM 调用类型分布</h4>
          <table class="atable">
            <thead><tr><th>类型</th><th>次数</th><th>成功</th><th>失败</th><th>成功率</th><th>Token(in/out)</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in al.ai_core.llm.purposes" :key="k">
                <td>{{ llmPurposeLabel(k) }}</td>
                <td>{{ v.total }}</td>
                <td>{{ v.ok }}</td>
                <td>{{ v.fail }}</td>
                <td>{{ (v.success_rate * 100).toFixed(0) }}%</td>
                <td>{{ (v.tokens_in || 0).toLocaleString() }} / {{ (v.tokens_out || 0).toLocaleString() }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- AI 核心 · 编排成功率 -->
        <div class="block" v-if="al.orchestration && al.orchestration.total">
          <h4>AI 核心 · 多意图编排</h4>
          <div class="kv">
            <span>编排总次数</span><b>{{ al.orchestration.total }}</b>
            <span>平均子任务数</span><b>{{ al.orchestration.split_count ? al.orchestration.split_count.avg.toFixed(1) : '-' }}</b>
            <span>平均成功率</span><b>{{ al.orchestration.success_rate ? (al.orchestration.success_rate.avg * 100).toFixed(0) + '%' : '-' }}</b>
            <span>平均耗时</span><b>{{ al.orchestration.duration_ms ? fmtMs(al.orchestration.duration_ms.avg) : '-' }}</b>
          </div>
        </div>
      </template>

      <div v-if="!quality && !qualityLoading" class="muted">暂无质量数据</div>
    </section>

    <!-- 回放(③-a) -->
    <section v-else-if="activeTab === 'replay'" class="panel">
      <div class="bar">
        <h3>生成回放</h3>
        <button class="refresh" :disabled="tracesLoading" @click="fetchTraces">刷新</button>
      </div>
      <div v-if="!selectedTrace" class="trace-filters">
        <input v-model="traceFilters.user_id" class="vinput small" placeholder="user_id" @keyup.enter="fetchTraces" />
        <input v-model="traceFilters.project_id" class="vinput small" placeholder="project_id" @keyup.enter="fetchTraces" />
        <input v-model="traceFilters.conversation_id" class="vinput small" placeholder="conversation_id" @keyup.enter="fetchTraces" />
        <input v-model="traceFilters.trace_id" class="vinput" placeholder="trace_id / turn_id" @keyup.enter="fetchTraces" />
        <button class="refresh" :disabled="tracesLoading" @click="fetchTraces">搜索</button>
        <button class="mini-btn" @click="traceFilters = { user_id: '', project_id: '', conversation_id: '', trace_id: '' }; fetchTraces()">重置</button>
      </div>
      <div v-if="selectedTrace" class="block">
        <button class="back" @click="selectedTrace = null">← 返回列表</button>
        <p class="hint">
          Trace/turn: {{ selectedTrace.trace.trace_id }}
          | 用户: {{ selectedTrace.trace.user_id }}
          | 项目: {{ selectedTrace.trace.project_id ?? '-' }}
          | 会话: {{ selectedTrace.trace.conversation_id ?? '-' }}
          | 状态: {{ statusLabel(selectedTrace.trace.status) }}
          | 开始: {{ selectedTrace.trace.started_at?.slice(0, 19) || '-' }}
          | 结束: {{ (selectedTrace.trace.finished_at as string)?.slice(0, 19) || '-' }}
        </p>

        <!-- 后置 QC 单裁判详情(新 schema: result.scores = {dim: int(0-100)}) -->
        <div v-if="selectedTrace.qc && selectedTrace.qc.result?.scores && Object.keys(selectedTrace.qc.result.scores).length" class="block">
          <h3>
            后置 QC 单裁判
            <span class="pill">整体 {{ selectedTrace.qc.overall.toFixed(2) }}</span>
            <span v-if="selectedTrace.qc.needs_review" class="pill warn">需复核</span>
            <span v-if="selectedTrace.qc.partial" class="pill gray">评分失败</span>
            <span v-if="selectedTrace.qc.safety_risk && selectedTrace.qc.safety_risk !== 'low'" class="pill danger">{{ selectedTrace.qc.safety_risk }}</span>
          </h3>
          <table class="qctable">
            <thead><tr><th>维度</th><th>评分(0-100)</th></tr></thead>
            <tbody>
              <tr v-for="(v, d) in selectedTrace.qc.result.scores" :key="d">
                <td>{{ qcLabel(String(d)) }}</td>
                <td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="selectedTrace.qc.result.rationale" class="hint">评语: {{ selectedTrace.qc.result.rationale }}</p>
        </div>
        <div v-else-if="selectedTrace.qc" class="block muted">
          <h3>后置 QC 单裁判</h3>
          <p>该 trace 暂无六维打分（仅整体分 {{ selectedTrace.qc.overall.toFixed(2) }}）。</p>
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
          <div class="events-toolbar">
            <button class="mini-btn" @click="expandAllEvents">展开全部</button>
          </div>
          <div v-for="(e, i) in selectedTrace.events" :key="i" class="evt" :class="{ open: expandedEvents[i] }">
            <div class="evt-head" @click="toggleEvent(i)">
              <span class="ecaret">{{ expandedEvents[i] ? '▾' : '▸' }}</span>
              <span class="eseq">{{ e.seq }}</span>
              <span class="etype">{{ eventTypeLabel(e.event_type) }}</span>
              <span v-if="e.stage" class="estage">{{ e.stage }}</span>
              <span v-if="evtStatus(e.payload)" class="estatus" :class="e.payload && isObj(e.payload) && e.payload.status === 'error' ? 'err' : ''">{{ evtStatus(e.payload) }}</span>
              <span v-if="evtDuration(e.payload) != null" class="edur">{{ evtDuration(e.payload) }}ms</span>
              <span v-if="evtChangedCount(e.payload)" class="echanged">{{ evtChangedCount(e.payload) }} 字段变更</span>
            </div>
            <div v-if="expandedEvents[i] && e.payload" class="evt-detail">
              <template v-if="isObj(e.payload)">
                <div v-if="e.payload.io_in" class="io-block">
                  <div class="io-title">IN · 阶段进入时状态</div>
                  <pre class="io-json">{{ pretty(e.payload.io_in) }}</pre>
                </div>
                <div v-if="e.payload.io_out" class="io-block">
                  <div class="io-title">OUT · 阶段离开时状态</div>
                  <pre class="io-json">{{ pretty(e.payload.io_out) }}</pre>
                </div>
                <div v-if="evtChangedCount(e.payload)" class="changed-block">
                  <div class="io-title">变更字段</div>
                  <span v-for="c in e.payload.changed" :key="c" class="chip">{{ c }}</span>
                </div>
                <div v-if="e.payload.error" class="err-block">
                  <div class="io-title">错误</div>
                  <pre class="io-json">{{ pretty(e.payload.error) }}</pre>
                </div>
                <div v-if="e.payload.reason_code" class="meta-row">reason_code: {{ e.payload.reason_code }}</div>
                <div v-if="e.payload.output_refs" class="meta-row">output_refs: {{ pretty(e.payload.output_refs) }}</div>
                <div v-if="!e.payload.io_in && !e.payload.io_out" class="meta-row">
                  <pre class="io-json">{{ pretty(e.payload) }}</pre>
                </div>
              </template>
              <pre v-else class="io-json">{{ pretty(e.payload) }}</pre>
            </div>
          </div>
        </div>
        <p v-else class="muted">该 Trace 没有结构化事件</p>
      </div>
      <table v-else class="utable">
        <thead>
          <tr>
            <th>Trace/turn ID</th><th>用户</th><th>项目</th><th>会话</th>
            <th>用户输入</th><th>状态</th><th>QC</th><th>评分</th><th>开始</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in traces" :key="t.id" style="cursor:pointer;" @click="viewTrace(t.trace_id)">
            <td>{{ t.trace_id.slice(0, 12) }}</td>
            <td>{{ t.user_id }}</td>
            <td>{{ t.project_id ?? '-' }}</td>
            <td>{{ t.conversation_id ?? '-' }}</td>
            <td class="user-input">{{ (t as any).user_input || '-' }}</td>
            <td>{{ statusLabel(t.status) }}</td>
            <td>{{ t.qc_overall != null ? t.qc_overall.toFixed(1) : '-' }}</td>
            <td>{{ t.feedback_rating != null ? t.feedback_rating : '-' }}</td>
            <td>{{ t.started_at?.slice(0, 19) || '-' }}</td>
            <td>{{ (t.finished_at as string)?.slice(0, 19) || '-' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!traces.length && !tracesLoading" class="muted">暂无生成记录</p>
    </section>

    <!-- 前端分析(第7条: 原系统分析重命名为前端分析, 仅保留纯前端埋点) -->
    <section v-else-if="activeTab === 'frontend'" class="panel">
      <div class="bar"><h3>前端分析</h3><button class="refresh" :disabled="alLoading" @click="fetchAnalytics">刷新</button></div>
      <div v-if="al?.error" class="muted">加载失败: {{ al.error }}</div>
      <template v-else-if="al">
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
        <!-- 前端 UV / PV(R6) -->
        <div v-if="al.frontend_uv" class="block">
          <h4>前端访问概览（UV / PV）</h4>
          <div class="card-row">
            <div class="card"><div class="k">累计独立访客 (UV)</div><div class="v">{{ al.frontend_uv.total }}</div></div>
            <div class="card"><div class="k">今日独立访客 (UV)</div><div class="v">{{ al.frontend_uv.today }}</div></div>
          </div>
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
            <td>{{ u.account }}</td>
            <td>{{ u.display_name || '-' }}</td>
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
                :value="u.tier"
                @change="changeTier(u, ($event.target as HTMLSelectElement).value)"
              >
                <option value="free">free</option>
                <option value="pro">pro</option>
                <option value="max">max</option>
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

    <!-- 向量库可视化（超管专用） -->
    <section v-else-if="activeTab === 'vector' && isSuper" class="panel">
      <div class="bar">
        <h3>向量库管理</h3>
        <button class="refresh" :disabled="vectorLoading" @click="refreshVector">刷新</button>
      </div>
      <p class="hint">可视化浏览 / 语义检索 / 受限写（删点 · 录入 · 清空）。写操作需超管并二次确认，已留痕。</p>
      <p v-if="vectorError" class="ctrlmsg">{{ vectorError }}</p>

      <div class="vector-layout">
        <!-- 左：集合列表 -->
        <aside class="coll-list">
          <div v-if="vectorLoading" class="muted">加载中…</div>
          <div v-else-if="!vectorCollections.length" class="muted">无集合或 Chroma 不可达</div>
          <button
            v-for="c in vectorCollections"
            :key="c.name"
            class="coll-item"
            :class="{ on: selectedCollection === c.name }"
            @click="selectCollection(c.name)"
          >
            <span class="coll-name">{{ c.name }}</span>
            <span class="coll-count">{{ c.count }}</span>
          </button>
        </aside>

        <!-- 右：主操作区 -->
        <div v-if="!selectedCollection" class="coll-empty muted">← 选择左侧集合开始操作</div>
        <div v-else class="coll-main">
          <!-- 检索 + 过滤 -->
          <div class="block">
            <div class="subbar">
              <input v-model="queryText" class="vinput" placeholder="语义检索（输入自然语言）" @keyup.enter="runQuery" />
              <input v-model.number="queryTopK" class="vinput small" type="number" min="1" max="50" />
              <button class="refresh" :disabled="queryLoading" @click="runQuery">检索</button>
            </div>
            <div class="subbar">
              <input v-model="browseWhere" class="vinput" placeholder='where 过滤（JSON，如 {"kind":"intent"}）' />
              <input v-model.number="browseLimit" class="vinput small" type="number" min="1" max="200" />
              <button class="refresh" :disabled="browseLoading" @click="browseCollection">浏览</button>
              <button class="mini-btn" :disabled="browseLoading || browseOffset < browseLimit" @click="browsePrev">上一页</button>
              <button class="mini-btn" :disabled="browseLoading || !browseHasMore" @click="browseNext">下一页</button>
            </div>

            <!-- 检索命中 -->
            <div v-if="queryHits.length" class="block">
              <h4>检索命中（{{ queryHits.length }}）</h4>
              <table class="atable">
                <thead><tr><th>ID</th><th>距离</th><th>文本</th><th>元数据</th></tr></thead>
                <tbody>
                  <tr v-for="h in queryHits" :key="h.id">
                    <td class="mono">{{ h.id }}</td>
                    <td>{{ h.distance != null ? h.distance.toFixed(4) : '-' }}</td>
                    <td class="doc-cell">{{ h.text }}</td>
                    <td class="meta-cell">{{ fmtMeta(h.metadata) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- 批量操作 -->
            <div class="subbar">
              <span class="muted">已选 {{ selectedIds.size }} 个</span>
              <button class="mini-btn danger" :disabled="!selectedIds.size" @click="deletePoints([...selectedIds])">删除选中</button>
              <button class="mini-btn" @click="showAddPanel = !showAddPanel">录入点</button>
              <button class="mini-btn danger" @click="deleteByWhere">按 where 删除</button>
              <button class="mini-btn danger" @click="clearCollection">清空集合</button>
            </div>

            <!-- 录入面板 -->
            <div v-if="showAddPanel" class="block add-panel">
              <h4>录入向量点</h4>
              <p v-if="addError" class="ctrlmsg">{{ addError }}</p>
              <textarea v-model="addDocument" class="vtextarea" placeholder="文本内容（将被向量化）" rows="3"></textarea>
              <textarea v-model="addMetaRaw" class="vtextarea" placeholder='元数据 JSON，如 {"kind":"manual"}' rows="2"></textarea>
              <button class="refresh" @click="submitAdd">提交</button>
            </div>
          </div>

          <!-- 浏览点表格 -->
          <div class="block">
            <h4>向量点（{{ browsePoints.length }}）</h4>
            <div v-if="browseLoading" class="muted">加载中…</div>
            <table v-else class="atable">
              <thead>
                <tr><th></th><th>ID</th><th>文本</th><th>元数据</th><th>操作</th></tr>
              </thead>
              <tbody>
                <tr v-for="p in browsePoints" :key="p.id">
                  <td><input type="checkbox" :checked="selectedIds.has(p.id)" @change="toggleSelect(p.id)" /></td>
                  <td class="mono">{{ p.id }}</td>
                  <td class="doc-cell">{{ p.document }}</td>
                  <td class="meta-cell">{{ fmtMeta(p.metadata) }}</td>
                  <td>
                    <button class="mini-btn" @click="openPointDetail(p.id)">详情</button>
                    <button class="mini-btn danger" @click="deletePoints([p.id])">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 单点详情弹层 -->
      <div v-if="selectedPoint" class="modal-mask" @click.self="selectedPoint = null">
        <div class="modal">
          <div class="bar">
            <h4>向量点详情</h4>
            <button class="mini-btn" @click="selectedPoint = null">关闭</button>
          </div>
          <p class="mono">ID: {{ selectedPoint.id }}</p>
          <div class="block">
            <h5>文本</h5>
            <pre class="vpre">{{ selectedPoint.document }}</pre>
          </div>
          <div class="block">
            <h5>元数据</h5>
            <pre class="vpre">{{ JSON.stringify(selectedPoint.metadata, null, 2) }}</pre>
          </div>
          <div class="block" v-if="selectedPoint.embedding">
            <h5>原始向量（{{ selectedPoint.embedding.length }} 维，前 16 值）</h5>
            <pre class="vpre">{{ selectedPoint.embedding.slice(0, 16).map((x: number) => x.toFixed(4)).join(', ') }}</pre>
          </div>
          <div v-else class="block">
            <button class="mini-btn" @click="openPointDetail(selectedPoint!.id, true)">加载原始向量</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 系统规则（双轨：MySQL 原文 × 向量摘要，超管专用 CRUD） -->
    <section v-else-if="activeTab === 'sysrules' && isSuper" class="panel">
      <SystemRulesAdmin />
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
  border-color: var(--brand2, var(--brand-border));
  background: var(--brand-bg);
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
  color: var(--text);
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
  color: var(--text);
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
.kv b { color: var(--text); font-weight: 700; }
.pill {
  display: inline-block;
  margin: 2px 6px 2px 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--brand-bg);
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
  color: var(--text-2);
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
.events { max-height: 520px; overflow: auto; }
.trace-filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.trace-filters .vinput { flex: 1 1 160px; min-width: 0; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text-1); font-size: 13px; }
.trace-filters .vinput.small { flex: 0 0 120px; width: 120px; }
.events-toolbar { display: flex; justify-content: flex-end; margin-bottom: 6px; }
.mini-btn { border: 1px solid var(--border); background: var(--panel); color: var(--text-2); border-radius: 6px; padding: 2px 10px; font-size: 12px; cursor: pointer; }
.mini-btn:hover { border-color: var(--brand); color: var(--brand); }
.evt { border-bottom: 1px solid var(--border); font-size: 13px; }
.evt-head { display: flex; gap: 10px; align-items: center; padding: 6px 4px; cursor: pointer; border-radius: 6px; }
.evt-head:hover { background: var(--brand-bg); }
.evt.open .evt-head { background: var(--brand-bg); }
.ecaret { width: 12px; color: var(--muted); }
.eseq { width: 28px; color: var(--muted); text-align: right; }
.etype { width: 52px; font-weight: 600; color: var(--brand); }
.estage { color: var(--text-4); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.estatus { padding: 1px 8px; border-radius: 10px; background: var(--brand-bg); color: var(--brand); font-size: 11px; font-weight: 600; }
.estatus.err { background: rgba(220, 38, 38, 0.12); color: var(--err); }
.edur { color: var(--text-4); font-size: 11px; }
.echanged { color: #d97706; font-size: 11px; }
.evt-detail { padding: 0 4px 8px 52px; }
.io-block { margin: 6px 0; }
.io-title { font-size: 11px; font-weight: 700; color: var(--text-4); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px; }
.io-json { background: var(--code-bg, #0d1117); color: #c9d1d9; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.5; max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.changed-block { margin: 6px 0; }
.chip { display: inline-block; margin: 2px 4px 2px 0; padding: 1px 8px; border-radius: 10px; background: rgba(217, 119, 6, 0.14); color: #d97706; font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.err-block { margin: 6px 0; }
.meta-row { font-size: 12px; color: var(--text-4); margin: 4px 0; }
.ecomment { color: var(--muted); font-style: italic; margin-left: auto; }
.back { border: 1px solid var(--border); background: var(--panel); border-radius: 8px; padding: 4px 12px; cursor: pointer; font-size: 13px; margin-bottom: 8px; }
.db-grid { display: flex; gap: 12px; flex-wrap: wrap; }
.db-card { display: flex; flex-direction: column; gap: 4px; background: var(--panel); border: 1px solid var(--border); border-left: 3px solid #22c55e; border-radius: 10px; padding: 12px 14px; min-width: 220px; flex: 1; }
.db-card.err { border-left-color: var(--err); }
.db-head { display: flex; align-items: center; justify-content: space-between; }
.db-name { font-weight: 700; font-size: 14px; color: var(--text-2); text-transform: uppercase; }
.db-stat { font-size: 12px; font-weight: 600; }
.db-stat.ok { color: #22c55e; }
.db-stat.err { color: var(--err); }
.db-cap { display: flex; align-items: baseline; gap: 8px; }
.db-cap-val { font-size: 20px; font-weight: 700; color: var(--text); }
.db-cap-pct { font-size: 14px; font-weight: 700; color: var(--brand); }
.db-cap-pct.none { color: var(--muted); }
.db-cap-detail { font-size: 12px; color: var(--muted); }
.db-extra { font-size: 12px; color: var(--text-3); margin-top: 2px; }
.db-colls { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.db-colls .pill { background: var(--brand-bg-soft); color: var(--brand); }

/* 服务器系统状态区块 */
.sys-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.sys-card { display: flex; flex-direction: column; gap: 4px; background: var(--panel); border: 1px solid var(--border); border-left: 3px solid var(--brand, #15c4a4); border-radius: 10px; padding: 12px 14px; }
.sys-k { font-size: 12px; color: var(--muted); }
.sys-v { font-size: 22px; font-weight: 700; color: var(--text); line-height: 1.2; }
.sys-v .warn { color: #ef8c3b; }
.sys-sub { font-size: 12px; color: var(--text-3); }
.sys-sub.err { color: var(--err); }
.sys-parts { margin-top: 14px; }
.sys-parts .warn { color: #ef8c3b; font-weight: 700; }

/* R1: API 延迟子标签(业务端 / 需求端) */
.subtabs { display: flex; gap: 8px; margin-bottom: 10px; }
.subtabs button { border: 1px solid var(--border); background: var(--panel); border-radius: 8px; padding: 5px 14px; cursor: pointer; font-size: 13px; color: var(--muted); }
.subtabs button.on { color: var(--brand); border-color: var(--brand2, var(--brand-border)); background: var(--brand-bg); font-weight: 600; }

/* 系统分析表 */
.atable { width: 100%; border-collapse: collapse; font-size: 13px; }
.atable th { text-align: left; padding: 6px 8px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }
.atable td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.atable .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
.rate-bar { display: flex; align-items: center; gap: 12px; font-size: 22px; font-weight: 700; color: var(--text); position: relative; padding: 10px 0; }
.rate-bar::before { content: ''; position: absolute; bottom: 0; left: 0; height: 4px; border-radius: 2px; background: linear-gradient(90deg, #22c55e var(--rate), var(--err-bg) var(--rate)); width: 100%; }
.rate-sub { font-size: 13px; color: var(--muted); font-weight: 400; }
h4 { margin: 12px 0 8px; font-size: 14px; color: var(--text); }
.reset-log { white-space: pre-wrap; font-size: 12px; background: var(--err-bg); border: 1px solid var(--err-border); border-radius: 8px; padding: 10px 12px; margin-top: 10px; color: #991b1b; line-height: 1.6; }

/* QC 雷达 + 复盘详情(v0.8.5 M1) */
.qc-radar { display: flex; flex-direction: column; align-items: center; }
.qc-radar h3 { align-self: flex-start; }
.pill { display: inline-block; font-size: 11px; font-weight: 700; padding: 1px 8px; border-radius: 999px; background: var(--violet-bg); color: #6d28d9; margin-left: 6px; }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.danger { background: var(--err-bg); color: var(--err); }
.pill.gray { background: var(--surface-3); color: var(--text-4); }
.qctable { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.qctable th { text-align: left; padding: 6px 8px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }
.qctable td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.fb-row { font-size: 13px; margin: 4px 0; }
.fb-dims { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.fb-dim { font-size: 12px; background: var(--surface-3); border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; color: var(--text-3); }
.msg { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin: 6px 0; }
.msg.user { background: var(--brand-bg); }
.msg.assistant { background: var(--surface-2); }
.msg-role { font-size: 11px; color: var(--muted); font-weight: 600; margin-bottom: 2px; }
.msg-body { font-size: 13px; white-space: pre-wrap; word-break: break-word; max-height: 280px; overflow: auto; }
.model-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.model-table th { text-align: left; padding: 6px 10px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; }
.model-table td { padding: 6px 10px; border-bottom: 1px solid var(--border); }
.model-table .mname { font-weight: 600; color: var(--primary, #15c4a4); }
.user-input { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--muted); }

/* ── 向量库工具 ── */
.vector-layout { display: flex; gap: 16px; align-items: flex-start; }
.coll-list { width: 220px; flex: 0 0 220px; max-height: 600px; overflow: auto; display: flex; flex-direction: column; gap: 6px; padding: 8px; border: 1px solid var(--border); border-radius: 12px; background: var(--panel); }
.coll-item { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--text-2); cursor: pointer; text-align: left; transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1); }
.coll-item:hover { border-color: var(--brand); color: var(--brand); }
.coll-item.on { background: var(--brand-bg); border-color: var(--brand); color: var(--brand); font-weight: 600; }
.coll-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.coll-count { font-size: 12px; color: var(--muted); background: var(--surface-2); border-radius: 6px; padding: 1px 7px; flex: 0 0 auto; }
.coll-empty { padding: 30px; }
.coll-main { flex: 1 1 auto; min-width: 0; }
.subbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.vinput { flex: 1 1 200px; min-width: 0; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text-1); font-size: 13px; }
.vinput.small { flex: 0 0 64px; width: 64px; }
.vtextarea { width: 100%; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text-1); font-size: 13px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin-bottom: 8px; }
.doc-cell { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.doc-cell:hover { white-space: normal; }
.meta-cell { max-width: 220px; font-size: 11px; color: var(--muted); word-break: break-all; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.add-panel { border: 1px solid var(--border); border-radius: 10px; padding: 12px; background: var(--surface-2); }
.vpre { max-height: 240px; overflow: auto; font-size: 12px; background: var(--surface-2); padding: 8px; border-radius: 8px; border: 1px solid var(--border); white-space: pre-wrap; word-break: break-word; }
.mini-btn.danger { border-color: rgba(220, 38, 38, 0.35); color: var(--err); }
.mini-btn.danger:hover { background: rgba(220, 38, 38, 0.12); border-color: var(--err); }
.modal-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5); display: flex; align-items: center; justify-content: center; z-index: 50; backdrop-filter: blur(4px); }
.modal { width: min(680px, 92vw); max-height: 86vh; overflow: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 18px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35); }
.modal .block { margin-top: 10px; }
.modal h5 { margin: 10px 0 4px; font-size: 13px; color: var(--text-4); }
</style>
