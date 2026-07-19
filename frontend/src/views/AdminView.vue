<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { get, post } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { ROLE_LABELS, type AdminUser, type MetricsSnapshot, type Role } from '../types'

const auth = useAuthStore()
const isSuper = computed(() => auth.user?.role === 'super_admin')
const currentRoleLabel = computed(
  () => ROLE_LABELS[(auth.user?.role as Role) || 'user'] || auth.user?.role || '-',
)

// ---- 标签页(RBAC:用户管理 / 控制面 仅超管可见) ----
type Tab = 'metrics' | 'users' | 'control' | 'quality' | 'replay'
const tabs: { key: Tab; label: string; superOnly: boolean }[] = [
  { key: 'metrics', label: '运行指标', superOnly: false },
  { key: 'quality', label: 'AI 质量', superOnly: false },
  { key: 'replay', label: '回放', superOnly: false },
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

// ---- 回放(③-a) ----
interface TraceItem {
  id: number; trace_id: string; user_id: number; model_id: string | null
  status: string; total_tokens: number; started_at: string | null; finished_at: string | null
}
interface TraceEventItem {
  seq: number; event_type: string; stage: string | null
  payload: unknown; created_at: string | null
}
interface TraceDetail {
  trace: TraceItem
  events: TraceEventItem[]
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

function statusLabel(s: string) {
  const m: Record<string, string> = { running: '生成中', done: '完成', error: '错误', aborted: '已取消' }
  return m[s] || s
}

function eventTypeLabel(t: string) {
  const m: Record<string, string> = { node: '节点', think: '思考', plan: '计划', token: '输出', error: '错误', done: '完成', aborted: '取消', degraded: '降级' }
  return m[t] || t
}

onMounted(() => {
  connectMetrics()
  if (isSuper.value) fetchUsers()
  fetchQuality()
  fetchTraces()
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
      <div class="block" v-if="dbItems.length">
        <h3>数据库状态</h3>
        <div class="db-grid">
          <div class="db-card" v-for="item in dbItems" :key="item.key">
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
        <h3>模型用量</h3>
        <div v-if="totalModelUsage === 0" class="muted">暂无数据</div>
        <ul v-else class="usage">
          <li v-for="(cnt, model) in metrics.model_usage" :key="model">
            <span class="mname">{{ model }}</span>
            <span class="mbar">
              <span
                class="mfill"
                :style="{ width: totalModelUsage ? (cnt / totalModelUsage) * 100 + '%' : '0%' }"
              ></span>
            </span>
            <span class="mcnt">{{ cnt }}</span>
          </li>
        </ul>
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
          <tr><th>Trace ID</th><th>模型</th><th>状态</th><th>Token</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="t in traces" :key="t.id" style="cursor:pointer;" @click="viewTrace(t.trace_id)">
            <td>{{ t.trace_id.slice(0, 12) }}</td>
            <td>{{ t.model_id || '-' }}</td>
            <td>{{ statusLabel(t.status) }}</td>
            <td>~{{ t.total_tokens }}</td>
            <td>{{ t.started_at?.slice(0, 19) || '-' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!traces.length && !tracesLoading" class="muted">暂无生成记录</p>
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
</style>
