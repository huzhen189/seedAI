<script setup lang="ts">
// ChatView —— 对话主页面(核心组件)。
// 职责:
//   1. 组装「项目 → 会话 → 消息」三级数据(经 project / conversation 两个 Pinia store);
//   2. 发送:校验登录态与项目/会话,调用 startChat 建立 SSE 流,把流事件映射为本地状态;
//   3. 思考面板:每个 agent 节点作为时间线的一步(精准分步反馈),Planner 的「计划/目标」
//      作为特殊节点卡片渲染;think 文本按阶段分别累积;
//   4. 断线续传 / 重连:用 sessionStorage 记录 active trace(convId+traceId),发送时写入、
//      done/aborted/error 清除;刷新或切换会话时若仍有未完成的 trace,以同一 traceId 重开
//      SSE 全量回放 + 续活(后端 stream_exists 命中则续接,Worker 后台独立继续);
//   5. 鉴权门禁:未登录时点发送 -> 记 pendingSend 并弹登录框;登录成功后自动重发;
//   6. 取消:stop() 级联 cancelChat -> 业务 -> AI 中断生成(C1);
//   7. 评价:生成完成后可对本次 trace 投 👍/👎。
// 左栏是对话区 + 思考轨迹,右栏是实时预览(PreviewPane)。
import { computed, onMounted, ref, watch } from 'vue'
import ThoughtTrail from '../components/ThoughtTrail.vue'
import PreviewPane from '../components/PreviewPane.vue'
import ChatInput from '../components/ChatInput.vue'
import MessageBubble from '../components/MessageBubble.vue'
import { useRouter } from 'vue-router'
import { startChat, cancelChat, fetchModels, sendFeedback, type ChatCallbacks } from '../api/chat'
import { useAuth } from '../composables/useAuth'
import { warmupWebLLM } from '../composables/useWebLLM'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import type { ModelInfo, PlanEvent, RetryEvent, ThoughtStep } from '../types'

const STAGE_LABELS: Record<string, string> = {
  enter_router: '路由分发',
  dispatch: '技能调度',
  enter_planner: '规划需求',
  enter_coder: '编写代码',
  enter_reviewer: '评审校验',
  previewing: '投递预览',
  preview: '生成预览',
  done: '完成',
}

// ---- 本地 UI 状态 ----
const models = ref<ModelInfo[]>([])
const model = ref('hy3')
const input = ref('')
const generating = ref(false)
const finished = ref(false)

// 思考时间线(每步一个 agent 节点)+ 计划特殊节点;替代旧版混成一坨的 thinks 字符串。
const thoughtSteps = ref<ThoughtStep[]>([])
const planNodes = ref<PlanEvent[]>([])
const currentStage = ref('')
const degraded = ref(false)
const generatedHtml = ref('')
const previewUrl = ref<string | null>(null)
const errorMsg = ref('')

// 生成产物文件列表(供右侧文件面板)
const generatedFiles = computed(() => {
  const files: { name: string; size: number; url?: string }[] = []
  if (generatedHtml.value) {
    files.push({
      name: 'index.html',
      size: new Blob([generatedHtml.value]).size,
      url: previewUrl.value || undefined,
    })
  }
  return files
})

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1048576).toFixed(1) + ' MB'
}

function downloadHtml() {
  if (!generatedHtml.value) return
  const blob = new Blob([generatedHtml.value], { type: 'text/html' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'index.html'
  a.click()
  URL.revokeObjectURL(url)
}
const traceId = ref('')
const esRef = ref<EventSource | null>(null)
const rating = ref(0)
const rateComment = ref('')
const rateSubmitted = ref(false)

const pendingSend = ref(false)
const pendingRetry = ref<{ suggested: string[]; message: string } | null>(null)
const lastSentText = ref('')

const auth = useAuth()
const projectStore = useProjectStore()
const convStore = useConversationStore()
const router = useRouter()
// TODO(④-b): 当 isLocalModel 为 true 时,Planner 走本地 WebLLM, Coder 仍走云端
// const isLocalModel = computed(() => model.value === 'local-webllm')

const messages = computed(() => convStore.messages)
const currentProjectName = computed(
  () =>
    projectStore.projects.find((p) => p.id === projectStore.currentProjectId)?.name || '未选择项目',
)

// 生成本次对话的链路 id(trace_id):
// 优先用 crypto.randomUUID(安全随机);老浏览器无该 API 时退化为"时间戳+随机"拼接。
function genTraceId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 't' + Date.now().toString(16) + Math.random().toString(16).slice(2)
}

// ---- 重连状态(sessionStorage,刷新同标签页可恢复) ----
const ACTIVE_KEY = 'seedai:active-gen'
function setActiveGen(convId: number, tid: string) {
  try {
    sessionStorage.setItem(ACTIVE_KEY, JSON.stringify({ convId, traceId: tid }))
  } catch {
    /* 忽略 */
  }
}
function clearActiveGen() {
  try {
    sessionStorage.removeItem(ACTIVE_KEY)
  } catch {
    /* 忽略 */
  }
}
function getActiveGen(): { convId: number; traceId: string } | null {
  try {
    const raw = sessionStorage.getItem(ACTIVE_KEY)
    if (!raw) return null
    const o = JSON.parse(raw)
    if (o && typeof o.convId === 'number' && o.traceId) return o
  } catch {
    /* 忽略 */
  }
  return null
}

// 重置「本轮生成」的展示态(不含 messages 数组,数组在 send/resume 各自处理)。
function resetGenState() {
  thoughtSteps.value = []
  planNodes.value = []
  currentStage.value = ''
  degraded.value = false
  generatedHtml.value = ''
  previewUrl.value = null
  errorMsg.value = ''
  finished.value = false
  rating.value = 0
  rateComment.value = ''
  rateSubmitted.value = false
  pendingRetry.value = null
}

function upsertStep(stage: string, status: ThoughtStep['status']) {
  const label = STAGE_LABELS[stage] || stage
  const existing = thoughtSteps.value.find((s) => s.stage === stage)
  if (existing) existing.status = status
  else thoughtSteps.value.push({ stage, label, status, think: '' })
}
function appendThink(stage: string, content: string) {
  const step = thoughtSteps.value.find((s) => s.stage === stage)
  if (step) step.think += content
}
function findAssistantIdx(): number {
  for (let i = convStore.messages.length - 1; i >= 0; i--) {
    if (convStore.messages[i].role === 'assistant') return i
  }
  return -1
}

// 统一的 SSE 事件回调:把 node/think/plan/token 映射到本地状态。
function makeCallbacks(assistantIdx: number): ChatCallbacks {
  return {
    onNode: (d) => {
      if (!d.stage) return
      currentStage.value = d.stage
      // 之前进行中的步骤标记为完成
      thoughtSteps.value.forEach((s) => {
        if (s.status === 'active') s.status = 'done'
      })
      if (d.stage === 'done') {
        thoughtSteps.value.forEach((s) => (s.status = 'done'))
      } else {
        upsertStep(d.stage, 'active')
      }
      if (d.stage === 'preview' && d.url) previewUrl.value = d.url as string
    },
    onThink: (d) => {
      // think 事件的阶段名不带 enter_ 前缀(planner / reviewer),
      // 需映射到时间线里的节点步名(enter_planner / enter_reviewer)。
      const THINK_TO_STEP: Record<string, string> = {
        planner: 'enter_planner',
        reviewer: 'enter_reviewer',
      }
      const stage = d.stage || currentStage.value
      const stepStage = THINK_TO_STEP[stage] || stage
      if (stepStage) appendThink(stepStage, d.content || '')
      if (stage === 'reviewer') {
        const step = thoughtSteps.value.find((s) => s.stage === 'enter_reviewer')
        if (step) {
          step.passed = d.passed
          step.comment = d.comment
        }
      }
    },
    onPlan: (d) => {
      planNodes.value.push({ title: d.title, goal: d.goal, steps: d.steps })
    },
    onToken: (t) => {
      generatedHtml.value += t
      const m = convStore.messages[assistantIdx]
      if (m) m.content += t
    },
    onPreview: (d) => {
      if (d.url) previewUrl.value = d.url as string
    },
    onDegraded: () => {
      degraded.value = true
    },
    onDone: () => {
      generating.value = false
      finished.value = true
      clearActiveGen()
      convStore.loadConversations(projectStore.currentProjectId!)
    },
    onAborted: () => {
      generating.value = false
      finished.value = true
      errorMsg.value = '已取消'
      clearActiveGen()
    },
    onRetry: (d: RetryEvent) => {
      generating.value = false
      finished.value = true
      clearActiveGen()
      pendingRetry.value = {
        suggested: d.suggested || [],
        message: d.message || '模型不可用',
      }
    },
    onError: (m) => {
      generating.value = false
      finished.value = true
      errorMsg.value = m
      clearActiveGen()
    },
  }
}

async function loadCurrentProject() {
  const pid = projectStore.currentProjectId
  if (pid == null) return
  await convStore.loadConversations(pid)
  if (convStore.conversations.length) {
    await convStore.loadMessages(convStore.conversations[0].id)
  } else {
    convStore.messages = []
  }
}

async function newConversation() {
  const pid = projectStore.currentProjectId
  if (pid == null) {
    alert('请先在左侧新建项目')
    return
  }
  await convStore.create(pid)
  generatedHtml.value = ''
  previewUrl.value = null
}

async function onConvChange(e: Event) {
  const id = Number((e.target as HTMLSelectElement).value)
  if (id) {
    await convStore.loadMessages(id)
    generatedHtml.value = ''
    previewUrl.value = null
    await maybeResume()
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || generating.value) return
  // 鉴权门禁:未登录不发送,记 pendingSend 并弹登录框。
  if (!auth.user.value) {
    pendingSend.value = true
    auth.openLogin()
    return
  }
  const pid = projectStore.currentProjectId
  if (pid == null) {
    alert('请先在左侧新建项目')
    return
  }
  // 会话不存在则按需自动创建(标题取首条消息前 20 字)。
  if (convStore.currentConvId == null) {
    await convStore.create(pid, text.slice(0, 20))
  }

  resetGenState()
  traceId.value = genTraceId()
  const cid = convStore.currentConvId!
  setActiveGen(cid, traceId.value)

  // 乐观更新:先把用户消息 + 一个空 assistant 占位塞进消息列表,
  // 后续 token 事件直接累加到 assistant 占位上,实现"打字机"效果。
  convStore.messages.push({
    role: 'user',
    content: text,
    conversation_id: cid,
    id: 0,
    created_at: '',
    model_id: model.value,
  } as any)
  convStore.messages.push({
    role: 'assistant',
    content: '',
    conversation_id: cid,
    id: 0,
    created_at: '',
    model_id: model.value,
  } as any)
  const assistantIdx = convStore.messages.length - 1
  generating.value = true
  lastSentText.value = text
  input.value = ''

  esRef.value = startChat({
    model: model.value,
    messages: convStore.messages.map((m) => ({
      role: m.role as 'user' | 'assistant',
      content: m.content,
    })),
    traceId: traceId.value,
    conversationId: cid,
    cb: makeCallbacks(assistantIdx),
  })
}

// 重连:以同一 traceId 重开 SSE 全量回放(后端命中 stream_exists 则续接,不重新生成)。
async function resume(convId: number, tid: string) {
  resetGenState()
  // 已加载的 assistant 消息内容清空,交由回放重新累积(避免重复)。
  const idx = findAssistantIdx()
  if (idx >= 0) convStore.messages[idx].content = ''
  generating.value = true
  traceId.value = tid
  setActiveGen(convId, tid)
  esRef.value = startChat({
    model: model.value,
    messages: convStore.messages.map((m) => ({
      role: m.role as 'user' | 'assistant',
      content: m.content,
    })),
    traceId: tid,
    conversationId: convId,
    cb: makeCallbacks(idx),
  })
}

// 若当前存在未完成的 active 生成,则重连恢复(刷新/切会话时调用)。
async function maybeResume() {
  const ag = getActiveGen()
  if (!ag || generating.value) return
  if (convStore.currentConvId !== ag.convId) {
    await convStore.loadMessages(ag.convId)
  }
  resume(ag.convId, ag.traceId)
}

async function stop() {
  if (!generating.value) return
  generating.value = false
  // 级联取消(C1):通知业务 /api/cancel -> 业务转发 AI /cancel -> Worker 中断生成。
  if (traceId.value) await cancelChat(traceId.value)
  esRef.value?.close()
  esRef.value = null
}

async function rate(val: number) {
  rating.value = val
}

async function submitRate() {
  if (rating.value < 1 || rating.value > 10) return
  rateSubmitted.value = true
  const ok = await sendFeedback(
    traceId.value,
    rating.value,
    convStore.currentConvId ?? undefined,
    rateComment.value || undefined,
  )
  if (!ok) rateSubmitted.value = false
}

function copyPreviewLink() {
  const url = previewUrl.value
  if (!url) return
  navigator.clipboard.writeText(url).catch(() => {
    // fallback: select and copy
    const t = document.createElement('textarea')
    t.value = url
    document.body.appendChild(t)
    t.select()
    document.execCommand('copy')
    document.body.removeChild(t)
  })
  alert('预览链接已复制到剪贴板')
}

onMounted(async () => {
  // 启动顺序:先恢复登录态(/auth/me) -> 拉模型列表(公开) -> 登录后才加载项目/会话
  await auth.init()
  const m = await fetchModels()
  if (m.length) models.value = m
  // ④-b 本地 WebLLM 选项(仅 Planner, Coder 走云端;在 useWebLLM.isWebGPUSupported() 时可用)
  models.value.push({ id: 'local-webllm', label: '本地 WebLLM(Planner)' })
  // 后台预取 WebLLM 模型权重(首屏空闲触发,幂等)
  warmupWebLLM()
  if (auth.user.value) {
    await projectStore.load()
    await loadCurrentProject()
    await maybeResume()
  }
})

watch(
  () => projectStore.currentProjectId,
  async (id) => {
    if (id != null) {
      await convStore.loadConversations(id)
      if (convStore.conversations.length) await convStore.loadMessages(convStore.conversations[0].id)
      else convStore.messages = []
      await maybeResume()
    }
  },
)

watch(
  () => convStore.pendingConvId,
  async (id) => {
    if (id != null) {
      await convStore.loadMessages(id)
      convStore.pendingConvId = null
      await maybeResume()
    }
  },
)

watch(
  () => auth.user,
  (u) => {
    // 登录成功后关闭登录框,加载项目/会话;若此前是"未登录点发送"则自动重发
    if (u) {
      auth.closeLogin()
      projectStore.load().then(() => loadCurrentProject())
      if (pendingSend.value) {
        pendingSend.value = false
        send()
      }
    }
  },
)

// 模型不可用时弹框让用户选替代模型
async function handleRetryChoice(newModel: string) {
  if (!pendingRetry.value) return
  pendingRetry.value = null
  // 移除上次发送失败的消息(user + 空 assistant)
  const msgs = convStore.messages
  while (msgs.length && msgs[msgs.length - 1].role === 'assistant') msgs.pop()
  if (msgs.length && msgs[msgs.length - 1].role === 'user') msgs.pop()
  model.value = newModel
  // 恢复原输入并重新发送
  const text = lastSentText.value
  if (!text) return
  input.value = text
  send()
}

watch(pendingRetry, (r) => {
  if (!r || !r.suggested.length) return
  const first = r.suggested[0]
  const show = window.confirm(
    `${r.message}\n\n是否切换到「${first}」重试？` +
    (r.suggested.length > 1 ? `\n(也可手动选择: ${r.suggested.join(', ')})` : ''),
  )
  if (show) handleRetryChoice(first)
  else pendingRetry.value = null
})
</script>

<template>
  <div class="chat">
    <div class="left-col">
      <div class="conv-bar">
        <span class="proj">📁 {{ currentProjectName }}</span>
        <select :value="convStore.currentConvId ?? ''" @change="onConvChange">
          <option value="" disabled>选择会话</option>
          <option v-for="c in convStore.conversations" :key="c.id" :value="c.id">
            {{ c.title || '会话' }}
          </option>
        </select>
        <button class="newconv" @click="newConversation">＋ 新建会话</button>
      </div>

      <div class="conv">
        <div v-if="messages.length === 0" class="empty">
          在下方描述你想生成的网站，AI 会先规划需求，再流式产出并实时预览。
        </div>
        <MessageBubble v-for="(m, i) in messages" :key="i" :role="m.role" :content="m.content" />
      </div>

      <div v-if="thoughtSteps.length || planNodes.length" class="trail-wrap">
        <ThoughtTrail
          :steps="thoughtSteps"
          :plans="planNodes"
          :degraded="degraded"
          :current="currentStage"
        />
      </div>

      <div class="footer">
        <ChatInput
          v-model:value="input"
          v-model:model="model"
          :generating="generating"
          :models="models"
          @send="send"
          @stop="stop"
          @open-settings="router.push('/settings')"
        />
        <div v-if="errorMsg" class="error">⚠ {{ errorMsg }}</div>
        <div v-if="finished && !errorMsg && (generatedHtml || previewUrl)" class="feedback">
          <span class="rate-label">评分 (1-10):</span>
          <template v-for="n in 10" :key="n">
            <button
              :class="{ on: rating >= n, sel: rating === n }"
              class="star-btn"
              @click="rate(n)"
            >
              {{ rating >= n ? '★' : '☆' }}
            </button>
          </template>
          <input
            v-if="rating >= 1 && !rateSubmitted"
            v-model="rateComment"
            class="comment-inp"
            placeholder="写点评语（可选）"
          />
          <button
            v-if="rating >= 1 && !rateSubmitted"
            class="submit-rate"
            @click="submitRate"
          >
            提交评分
          </button>
          <span v-if="rateSubmitted" class="rated">已评价 {{ rating }} 分 ✓</span>
          <button
            v-if="previewUrl"
            class="copy-link"
            title="复制预览链接"
            @click="copyPreviewLink"
          >
            🔗 复制预览链接
          </button>
          <a v-if="previewUrl" :href="previewUrl" target="_blank" rel="noreferrer" class="open">
            打开线上预览 ↗
          </a>
        </div>
      </div>
    </div>

    <div class="right-pane">
      <!-- 生成产物文件面板 -->
      <div v-if="generatedFiles.length || generating" class="artifact-panel">
        <div class="artifact-head">📁 生成产物</div>
        <div v-if="generating" class="artifact-empty">AI 正在生成…</div>
        <div v-for="f in generatedFiles" :key="f.name" class="artifact-file">
          <span class="af-name">📄 {{ f.name }}</span>
          <span class="af-size">{{ formatFileSize(f.size) }}</span>
          <a v-if="f.url" :href="f.url" target="_blank" class="af-open" title="线上预览">🔗</a>
          <button v-if="!f.url && generatedHtml" class="af-dl" title="下载" @click="downloadHtml">⬇</button>
        </div>
      </div>
      <PreviewPane :html="generatedHtml" :url="previewUrl" :loading="generating" />
    </div>

  </div>
</template>

<style scoped>
.chat {
  flex: 1;
  display: flex;
  min-height: 0;
}
.left-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.right-pane {
  width: 46%;
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.artifact-panel {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
  min-height: 0;
  overflow-y: auto;
  max-height: 30%;
}
.artifact-head {
  font-size: 12px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  margin-bottom: 6px;
}
.artifact-empty {
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
}
.artifact-file {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  padding: 4px 6px;
  border-radius: 6px;
  background: #f8fafc;
}
.af-name { color: #334155; }
.af-size { color: var(--muted); font-size: 11px; margin-left: auto; }
.af-open { text-decoration: none; font-size: 13px; }
.af-dl { border: none; background: none; cursor: pointer; font-size: 13px; padding: 0 2px; }
.conv-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.conv-bar select {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px 8px;
  font-size: 13px;
}
.newconv {
  margin-left: auto;
  border: 1px solid var(--brand);
  background: var(--brand);
  color: #fff;
  border-radius: 8px;
  padding: 4px 12px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
.conv {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.empty {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
  background: var(--panel);
  border: 1px dashed var(--border);
  border-radius: 10px;
  padding: 14px;
}
.trail-wrap {
  max-height: 32%;
  overflow: auto;
  background: var(--panel);
  border-top: 1px solid var(--border);
  padding: 10px 14px;
}
.footer {
  padding: 12px 14px;
  background: var(--panel);
  border-top: 1px solid var(--border);
}
.error {
  color: var(--err);
  font-size: 13px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 8px;
}
.feedback {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--muted);
  margin-top: 8px;
}
.rate-label { margin-right: 4px; }
.star-btn {
  border: none;
  background: none;
  color: #d4d4d8;
  cursor: pointer;
  font-size: 16px;
  padding: 0 2px;
  transition: color .15s;
}
.star-btn.on { color: #f59e0b; }
.star-btn.sel { color: #e17800; }
.comment-inp {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 12px;
  width: 140px;
  color: var(--text);
  background: var(--panel);
}
.submit-rate {
  border: 1px solid var(--brand);
  background: var(--brand);
  color: #fff;
  border-radius: 6px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.rated { color: var(--brand2); font-weight: 600; }
.copy-link {
  border: 1px solid var(--brand);
  background: transparent;
  color: var(--brand);
  border-radius: 6px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  margin-left: 4px;
}
.feedback .open {
  margin-left: auto;
  color: var(--brand);
  text-decoration: none;
  font-weight: 600;
}
</style>
