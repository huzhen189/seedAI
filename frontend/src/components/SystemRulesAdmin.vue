<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { get, post, put, delJson } from '../api/client'

// ── 类型（与后端 services.system_rules.rule_obj_to_dict 对齐） ──
interface SystemRule {
  id: number
  rule_key: string
  scope: 'global' | 'domain' | 'user' | 'project' | 'session'
  scope_ref: string | null
  rule_type: 'constraint' | 'guardrail' | 'policy' | 'preference'
  title: string
  content: string
  summary: string
  keywords: string
  priority: number
  version: number
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

const SCOPES = ['global', 'domain', 'user', 'project', 'session'] as const
const RULE_TYPES = ['constraint', 'guardrail', 'policy', 'preference'] as const

// ── 列表 + 过滤 ──
const rules = ref<SystemRule[]>([])
const loading = ref(false)
const errMsg = ref('')
const filtScope = ref('')
const filtType = ref('')
const filtActive = ref<'all' | '1' | '0'>('all')
const filtQ = ref('')

async function fetchRules() {
  loading.value = true
  errMsg.value = ''
  try {
    const params = new URLSearchParams()
    if (filtScope.value) params.set('scope', filtScope.value)
    if (filtType.value) params.set('rule_type', filtType.value)
    if (filtActive.value !== 'all') params.set('is_active', filtActive.value)
    if (filtQ.value.trim()) params.set('q', filtQ.value.trim())
    rules.value = await get(`/admin/system-rules?${params.toString()}`)
  } catch (e: any) {
    errMsg.value = (e as Error).message || '加载失败'
  } finally {
    loading.value = false
  }
}

const filteredCount = computed(() => rules.value.length)

// ── 详情弹层 ──
const detail = ref<SystemRule | null>(null)

// ── 编辑 / 新增 弹层 ──
const editing = ref(false) // 是否处于编辑（false=新增）
const formOpen = ref(false)
const formErr = ref('')
const saving = ref(false)
const form = ref({
  rule_key: '',
  scope: 'global' as SystemRule['scope'],
  scope_ref: '' as string,
  rule_type: 'policy' as SystemRule['rule_type'],
  title: '',
  content: '',
  summary: '',
  keywords: '',
  priority: 50,
  is_active: true,
})

function openCreate() {
  editing.value = false
  form.value = {
    rule_key: '', scope: 'global', scope_ref: '', rule_type: 'policy',
    title: '', content: '', summary: '', keywords: '', priority: 50, is_active: true,
  }
  formErr.value = ''
  formOpen.value = true
}

function openEdit(r: SystemRule) {
  editing.value = true
  form.value = {
    rule_key: r.rule_key, scope: r.scope, scope_ref: r.scope_ref || '',
    rule_type: r.rule_type, title: r.title, content: r.content, summary: r.summary,
    keywords: r.keywords, priority: r.priority, is_active: r.is_active,
  }
  formErr.value = ''
  formOpen.value = true
}

const needRef = computed(() => form.value.scope !== 'global')

const KEY_RE = /^[A-Za-z0-9_:.\-]{1,64}$/

function validateForm(): string | null {
  if (!editing.value && !KEY_RE.test(form.value.rule_key)) {
    return 'rule_key 须为字母/数字/_/./-/:，长度 1-64'
  }
  if (!SCOPES.includes(form.value.scope)) return 'scope 非法'
  if (!RULE_TYPES.includes(form.value.rule_type)) return 'rule_type 非法'
  if (form.value.scope !== 'global' && !form.value.scope_ref.trim()) {
    return '非 global 作用域必须填写 scope_ref（域名/用户 id/项目 id）'
  }
  if (!form.value.title.trim()) return '标题不能为空'
  if (!form.value.summary.trim()) return '向量摘要(summary)不能为空'
  if (form.value.summary.length > 500) return '摘要不超过 500 字'
  if (!form.value.content.trim()) return '规则原文(content)不能为空'
  if (form.value.priority < 0 || form.value.priority > 100) return '优先级 0-100'
  return null
}

async function save() {
  const verr = validateForm()
  if (verr) { formErr.value = verr; return }
  saving.value = true
  formErr.value = ''
  const payload: Record<string, unknown> = {
    scope: form.value.scope,
    scope_ref: form.value.scope_ref.trim() || null,
    rule_type: form.value.rule_type,
    title: form.value.title.trim(),
    summary: form.value.summary.trim(),
    keywords: form.value.keywords.trim(),
    content: form.value.content,
    priority: Number(form.value.priority),
    is_active: form.value.is_active,
  }
  try {
    if (editing.value) {
      const key = encodeURIComponent(form.value.rule_key)
      await put(`/admin/system-rules/${key}`, payload)
    } else {
      payload.rule_key = form.value.rule_key.trim()
      await post('/admin/system-rules', payload)
    }
    formOpen.value = false
    await fetchRules()
  } catch (e: any) {
    formErr.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

// ── 删除 ──
async function removeRule(r: SystemRule) {
  if (!confirm(`确认删除规则「${r.summary}」(${r.rule_key})？此操作不可撤销，将从 MySQL 与向量库一并移除。`)) return
  try {
    await delJson(`/admin/system-rules/${encodeURIComponent(r.rule_key)}`)
    if (detail.value?.rule_key === r.rule_key) detail.value = null
    await fetchRules()
  } catch (e: any) {
    errMsg.value = (e as Error).message || '删除失败'
  }
}

// ── 重建向量索引 ──
const reindexing = ref(false)
const reindexMsg = ref('')
async function reindex() {
  if (!confirm('确认用全部活跃规则重建向量索引？用于修复向量/MySQL 漂移。')) return
  reindexing.value = true
  reindexMsg.value = ''
  try {
    const r = await post('/admin/system-rules/reindex', {})
    reindexMsg.value = `已重建 ${r.reindexed} 条向量索引`
  } catch (e: any) {
    reindexMsg.value = '重建失败: ' + ((e as Error).message || '')
  } finally {
    reindexing.value = false
  }
}

function fmtTime(t?: string | null) {
  return t ? t.slice(0, 19).replace('T', ' ') : '—'
}

// 徽标配色（scope / rule_type）
const scopeClass: Record<string, string> = {
  global: 'sc-global', domain: 'sc-domain', user: 'sc-user',
  project: 'sc-project', session: 'sc-session',
}
const typeClass: Record<string, string> = {
  constraint: 'tp-constraint', guardrail: 'tp-guardrail',
  policy: 'tp-policy', preference: 'tp-preference',
}

onMounted(fetchRules)
</script>

<template>
  <div class="sr">
    <div class="bar">
      <h3>系统规则（双轨：MySQL 原文 × 向量摘要）</h3>
      <button class="refresh" :disabled="loading" @click="fetchRules">刷新</button>
      <button class="act" :disabled="reindexing" @click="reindex">
        {{ reindexing ? '重建中…' : '重建向量索引' }}
      </button>
      <button class="act primary" @click="openCreate">+ 新增规则</button>
    </div>
    <p class="hint">
      列表展示<strong>向量库摘要</strong>；点击任意规则查看<strong>详情（标题=摘要，正文=MySQL 原文）</strong>。
      所有改动联动同步向量索引，并留痕审计。
    </p>
    <p v-if="reindexMsg" class="ctrlmsg">{{ reindexMsg }}</p>
    <p v-if="errMsg" class="errmsg">{{ errMsg }}</p>

    <!-- 过滤条 -->
    <div class="filters">
      <input v-model="filtQ" class="fin" placeholder="搜索摘要/标题/关键词" @keyup.enter="fetchRules" />
      <select v-model="filtScope" class="fsel" @change="fetchRules">
        <option value="">全部作用域</option>
        <option v-for="s in SCOPES" :key="s" :value="s">{{ s }}</option>
      </select>
      <select v-model="filtType" class="fsel" @change="fetchRules">
        <option value="">全部类型</option>
        <option v-for="t in RULE_TYPES" :key="t" :value="t">{{ t }}</option>
      </select>
      <select v-model="filtActive" class="fsel" @change="fetchRules">
        <option value="all">启用+禁用</option>
        <option value="1">仅启用</option>
        <option value="0">仅禁用</option>
      </select>
      <span class="count">共 {{ filteredCount }} 条</span>
    </div>

    <!-- 列表 -->
    <div v-if="loading" class="muted">加载中…</div>
    <table v-else-if="rules.length" class="sr-table">
      <thead>
        <tr>
          <th>向量摘要（标题）</th>
          <th>作用域</th>
          <th>类型</th>
          <th>优先级</th>
          <th>状态</th>
          <th>更新时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="r in rules"
          :key="r.id"
          class="row"
          :class="{ off: !r.is_active }"
          @click="detail = r"
        >
          <td class="sum">
            <div class="summary">{{ r.summary }}</div>
            <div class="key">{{ r.rule_key }}</div>
          </td>
          <td>
            <span class="badge" :class="scopeClass[r.scope]">{{ r.scope }}<template v-if="r.scope_ref">:{{ r.scope_ref }}</template></span>
          </td>
          <td><span class="badge" :class="typeClass[r.rule_type]">{{ r.rule_type }}</span></td>
          <td class="num">{{ r.priority }}</td>
          <td>
            <span class="dot" :class="r.is_active ? 'on' : 'off'"></span>
            {{ r.is_active ? '启用' : '禁用' }}
          </td>
          <td class="time">{{ fmtTime(r.updated_at) }}</td>
          <td class="ops" @click.stop>
            <button class="mini-btn" @click="openEdit(r)">编辑</button>
            <button class="mini-btn danger" @click="removeRule(r)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">暂无规则。点击「新增规则」创建。</p>

    <!-- 详情弹层（标题=摘要，正文=MySQL 原文） -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal">
        <div class="bar">
          <h4 class="d-title">{{ detail.summary }}</h4>
          <button class="mini-btn" @click="detail = null">关闭</button>
        </div>
        <div class="meta-row">
          <span class="badge" :class="scopeClass[detail.scope]">{{ detail.scope }}<template v-if="detail.scope_ref">:{{ detail.scope_ref }}</template></span>
          <span class="badge" :class="typeClass[detail.rule_type]">{{ detail.rule_type }}</span>
          <span class="pill">优先级 {{ detail.priority }}</span>
          <span class="pill">版本 v{{ detail.version }}</span>
          <span class="pill" :class="detail.is_active ? 'ok' : 'gray'">{{ detail.is_active ? '启用' : '禁用' }}</span>
        </div>
        <div class="kv">
          <span>rule_key</span><b class="mono">{{ detail.rule_key }}</b>
          <span>标题(title)</span><b>{{ detail.title }}</b>
          <span>更新时间</span><b>{{ fmtTime(detail.updated_at) }}</b>
        </div>
        <div class="block">
          <h5>规则原文（MySQL 真相）</h5>
          <pre class="vpre">{{ detail.content }}</pre>
        </div>
        <div class="block" v-if="detail.keywords">
          <h5>关键词（向量索引）</h5>
          <div class="kw">
            <span v-for="k in detail.keywords.split('|')" :key="k" class="chip">{{ k }}</span>
          </div>
        </div>
        <div class="modal-ops">
          <button class="act" @click="openEdit(detail)">编辑</button>
          <button class="act danger" @click="removeRule(detail)">删除</button>
        </div>
      </div>
    </div>

    <!-- 新增 / 编辑 弹层 -->
    <div v-if="formOpen" class="modal-mask" @click.self="formOpen = false">
      <div class="modal wide">
        <div class="bar">
          <h4>{{ editing ? '编辑规则' : '新增规则' }}</h4>
          <button class="mini-btn" @click="formOpen = false">关闭</button>
        </div>
        <p v-if="formErr" class="errmsg">{{ formErr }}</p>
        <div class="form">
          <label class="fld" :class="{ disabled: editing }">
            <span>rule_key（稳定锚，不可改）</span>
            <input v-model="form.rule_key" :disabled="editing" placeholder="如 global.no_secret_leak" />
          </label>
          <div class="grid2">
            <label class="fld">
              <span>作用域 scope</span>
              <select v-model="form.scope">
                <option v-for="s in SCOPES" :key="s" :value="s">{{ s }}</option>
              </select>
            </label>
            <label class="fld" :class="{ required: needRef }">
              <span>scope_ref{{ needRef ? '（必填）' : '（global 留空）' }}</span>
              <input v-model="form.scope_ref" :disabled="!needRef" :placeholder="needRef ? '如 chat / demo / 123' : '—'" />
            </label>
          </div>
          <div class="grid2">
            <label class="fld">
              <span>类型 rule_type</span>
              <select v-model="form.rule_type">
                <option v-for="t in RULE_TYPES" :key="t" :value="t">{{ t }}</option>
              </select>
            </label>
            <label class="fld">
              <span>优先级 priority（0-100）</span>
              <input v-model.number="form.priority" type="number" min="0" max="100" />
            </label>
          </div>
          <label class="fld">
            <span>标题(title)</span>
            <input v-model="form.title" placeholder="一句话名称" />
          </label>
          <label class="fld">
            <span>向量摘要(summary) — 列表标题 / 语义召回文本</span>
            <textarea v-model="form.summary" rows="2" maxlength="500" placeholder="一句话讲清这条规则（≤500字）"></textarea>
          </label>
          <label class="fld">
            <span>关键词(keywords) — 用 | 分隔，增强召回</span>
            <input v-model="form.keywords" placeholder="如 密钥|密码|token" />
          </label>
          <label class="fld">
            <span>规则原文(content) — 注入系统 Prompt 的完整文本（MySQL 真相）</span>
            <textarea v-model="form.content" rows="6" placeholder="完整规则文本…"></textarea>
          </label>
          <label class="chk">
            <input type="checkbox" v-model="form.is_active" />
            <span>启用（禁用后不进召回、不进 Prompt，但仍保留审计）</span>
          </label>
        </div>
        <div class="modal-ops">
          <button class="act primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
          <button class="act" @click="formOpen = false">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sr { display: flex; flex-direction: column; gap: 12px; }
.bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.bar h3 { margin: 0; font-size: 15px; color: var(--text); }
.refresh {
  margin-left: auto; border: 1px solid var(--border); background: var(--panel);
  border-radius: 8px; padding: 4px 12px; cursor: pointer; font-size: 13px; color: var(--muted);
}
.act {
  border: 1px solid var(--border); background: var(--panel); color: var(--text-2);
  border-radius: 8px; padding: 4px 12px; cursor: pointer; font-size: 13px;
}
.act:hover { border-color: var(--brand); color: var(--brand); }
.act.primary { border-color: var(--brand); background: var(--brand); color: #fff; font-weight: 600; }
.act.primary:hover { filter: brightness(1.05); }
.act.danger { border-color: rgba(220, 38, 38, 0.35); color: var(--err); }
.act.danger:hover { background: rgba(220, 38, 38, 0.12); border-color: var(--err); }
.hint { font-size: 12px; color: var(--muted); margin: 0; }
.hint strong { color: var(--text-2); }
.ctrlmsg { font-size: 13px; color: var(--brand); margin: 0; }
.errmsg { font-size: 13px; color: var(--err); margin: 0; }
.muted { color: var(--muted); font-size: 13px; }

.filters { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.fin {
  flex: 1 1 240px; min-width: 0; padding: 6px 10px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--surface-2); color: var(--text-1); font-size: 13px;
}
.fsel {
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface-2); color: var(--text-1); font-size: 13px;
}
.count { font-size: 12px; color: var(--muted); margin-left: auto; }

.sr-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.sr-table th {
  text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--border);
  color: var(--muted); font-weight: 600; font-size: 12px;
}
.sr-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
.row { cursor: pointer; transition: background 0.18s cubic-bezier(0.16, 1, 0.3, 1), transform 0.18s cubic-bezier(0.16, 1, 0.3, 1); }
.row:hover { background: var(--brand-bg); transform: translateY(-1px); }
.row.off { opacity: 0.6; }
.summary { font-size: 13px; color: var(--text); line-height: 1.4; }
.key { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--muted); margin-top: 2px; }
.num { font-variant-numeric: tabular-nums; }
.time { color: var(--muted); font-size: 12px; white-space: nowrap; }
.ops { white-space: nowrap; }
.mini-btn {
  border: 1px solid var(--border); background: var(--panel); color: var(--text-2);
  border-radius: 6px; padding: 2px 10px; font-size: 12px; cursor: pointer; margin-right: 4px;
}
.mini-btn:hover { border-color: var(--brand); color: var(--brand); }
.mini-btn.danger { border-color: rgba(220, 38, 38, 0.35); color: var(--err); }
.mini-btn.danger:hover { background: rgba(220, 38, 38, 0.12); border-color: var(--err); }

.badge {
  display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
  font-weight: 700; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.sc-global { background: var(--surface-3); color: var(--text-3); }
.sc-domain { background: rgba(21, 196, 164, 0.14); color: #15c4a4; }
.sc-user { background: rgba(124, 58, 237, 0.14); color: #8b5cf6; }
.sc-project { background: rgba(37, 99, 235, 0.14); color: #2563eb; }
.sc-session { background: rgba(217, 119, 6, 0.16); color: #d97706; }
.tp-constraint { background: rgba(220, 38, 38, 0.14); color: #dc2626; }
.tp-guardrail { background: rgba(234, 88, 12, 0.16); color: #ea580c; }
.tp-policy { background: rgba(37, 99, 235, 0.14); color: #2563eb; }
.tp-preference { background: rgba(22, 163, 74, 0.16); color: #16a34a; }

.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.dot.on { background: #22c55e; }
.dot.off { background: var(--muted); }

.pill {
  display: inline-block; margin: 2px 6px 2px 0; padding: 1px 8px; border-radius: 999px;
  background: var(--surface-3); color: var(--text-3); font-size: 11px; font-weight: 600;
}
.pill.ok { background: rgba(34, 197, 94, 0.16); color: #22c55e; }
.pill.gray { background: var(--surface-3); color: var(--muted); }

.modal-mask {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5); display: flex;
  align-items: center; justify-content: center; z-index: 60; backdrop-filter: blur(4px);
}
.modal {
  width: min(640px, 92vw); max-height: 88vh; overflow: auto; background: var(--panel);
  border: 1px solid var(--border); border-radius: 16px; padding: 18px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
  animation: pop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}
.modal.wide { width: min(720px, 94vw); }
@keyframes pop { from { transform: scale(0.96); opacity: 0; } to { transform: scale(1); opacity: 1; } }
.modal .bar { display: flex; align-items: center; gap: 10px; }
.d-title { margin: 0; font-size: 16px; color: var(--text); line-height: 1.4; }
.modal h5 { margin: 12px 0 6px; font-size: 12px; color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.meta-row { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin-bottom: 6px; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; font-size: 13px; align-items: center; margin: 8px 0; }
.kv span { color: var(--muted); }
.kv b { color: var(--text); font-weight: 700; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
.block { margin-top: 8px; }
.vpre {
  max-height: 280px; overflow: auto; font-size: 13px; line-height: 1.6; background: var(--surface-2);
  padding: 10px; border-radius: 8px; border: 1px solid var(--border); white-space: pre-wrap;
  word-break: break-word; color: var(--text-1); margin: 0;
}
.kw { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  background: rgba(217, 119, 6, 0.14); color: #d97706; font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.modal-ops { display: flex; gap: 10px; justify-content: flex-end; margin-top: 14px; }

.form { display: flex; flex-direction: column; gap: 12px; margin-top: 6px; }
.fld { display: flex; flex-direction: column; gap: 4px; }
.fld > span { font-size: 12px; color: var(--muted); }
.fld.required > span { color: var(--err); }
.fld.disabled { opacity: 0.65; }
.fld input, .fld select, .fld textarea {
  border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px;
  font-size: 13px; background: var(--surface-2); color: var(--text-1); font-family: inherit;
}
.fld textarea { resize: vertical; line-height: 1.5; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.chk { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-2); }
.chk input { width: 16px; height: 16px; }
@media (max-width: 600px) {
  .grid2 { grid-template-columns: 1fr; }
}
</style>
