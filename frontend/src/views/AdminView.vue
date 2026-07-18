<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { ROLE_LABELS, type AdminUser, type MetricsSnapshot, type Role } from '../types'

const auth = useAuthStore()
const isSuper = computed(() => auth.user?.role === 'super_admin')
const currentRoleLabel = computed(
  () => ROLE_LABELS[(auth.user?.role as Role) || 'user'] || auth.user?.role || '-',
)

// ---- 标签页(RBAC:用户管理 / 控制面 仅超管可见) ----
type Tab = 'metrics' | 'users' | 'control'
const tabs: { key: Tab; label: string; superOnly: boolean }[] = [
  { key: 'metrics', label: '运行指标', superOnly: false },
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
    const r = await fetch('/admin/users')
    if (r.ok) users.value = await r.json()
  } catch {
    /* 忽略 */
  } finally {
    usersLoading.value = false
  }
}

async function changeRole(u: AdminUser, role: string) {
  try {
    const r = await fetch(`/admin/users/${u.id}/role`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    })
    if (r.ok) {
      const updated = await r.json()
      u.role = updated.role
    } else {
      const e = await r.json().catch(() => ({}))
      alert(e.detail || '修改角色失败')
    }
  } catch {
    alert('网络错误')
  }
}

async function changePlan(u: AdminUser, plan: string) {
  try {
    const r = await fetch(`/admin/users/${u.id}/plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan }),
    })
    if (r.ok) {
      const updated = await r.json()
      u.plan = updated.plan
    } else {
      const e = await r.json().catch(() => ({}))
      alert(e.detail || '修改套餐失败')
    }
  } catch {
    alert('网络错误')
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

async function doScale() {
  try {
    const r = await fetch(
      `/admin/scale?name=${encodeURIComponent(scaleName.value)}&replicas=${scaleReplicas.value}`,
      { method: 'POST' },
    )
    const d = await r.json().catch(() => ({}))
    ctrlMsg.value = d.note || (d.ack ? '已提交扩缩容' : '操作失败')
  } catch {
    ctrlMsg.value = '网络错误'
  }
}
async function doStop() {
  try {
    const r = await fetch(`/admin/stop?name=${encodeURIComponent(stopName.value)}`, {
      method: 'POST',
    })
    const d = await r.json().catch(() => ({}))
    ctrlMsg.value = d.note || (d.ack ? '已提交停止' : '操作失败')
  } catch {
    ctrlMsg.value = '网络错误'
  }
}

onMounted(() => {
  connectMetrics()
  if (isSuper.value) fetchUsers()
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

    <!-- 用户管理(仅超管) -->
    <section v-else-if="activeTab === 'users' && isSuper" class="panel">
      <div class="bar">
        <h3>用户列表</h3>
        <button class="refresh" @click="fetchUsers" :disabled="usersLoading">刷新</button>
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
</style>
