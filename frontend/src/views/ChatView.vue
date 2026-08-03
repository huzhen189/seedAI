<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import ApprovalCard from '../components/ApprovalCard.vue'
import ChatInput from '../components/ChatInput.vue'
import MessageBubble from '../components/MessageBubble.vue'
import RightPanel from '../components/RightPanel.vue'
import ThinkingTrail from '../components/ThinkingTrail.vue'
import {
  controlTurn,
  getPendingApprovals,
  getTurn,
  replayStream,
  startChat,
  submitApproval,
  submitFeedback,
  type StreamSubscription,
} from '../api/chat'
import { createProject, listArtifacts } from '../api/projects'
import { useAuth } from '../composables/useAuth'
import { useConversationStore } from '../stores/conversation'
import { useProjectStore } from '../stores/project'
import {
  createStreamUiState,
  reduceStreamEvent,
  resetStreamUiState,
  type StreamUiState,
} from '../stream/reducer'
import { offlineQueue, onOnline, isOnline, type QueuedMessage } from '../stream/offlineQueue'
import type { Artifact, Message, ModelInfo } from '../types'
import type { StreamEvent } from '../types/contracts.generated'

// 模板内不允许直接使用 import.meta, 提为 script 常量供绑定。
const devMode = import.meta.env.DEV

interface ResumeRef {
  streamId: string
  turnId: string
  after: number
}

const RESUME_KEY = 'seedai:stream-resume'
const auth = useAuth()
const projectStore = useProjectStore()
const convStore = useConversationStore()
const stream = reactive(createStreamUiState()) as StreamUiState
const input = ref('')
const model = ref('deepseek')
const models = ref<ModelInfo[]>([])
const generating = ref(false)
const stopping = ref(false)
const replaying = ref(false)
const approvalSubmitting = ref(false)
const errorMessage = ref('')
const offlineNote = ref('')
const activeAssistant = ref<Message | null>(null)
const subscription = ref<StreamSubscription | null>(null)
const replaySubscription = ref<StreamSubscription | null>(null)
// M9c: 联网恢复回调的注销函数, 卸载时清理避免重复注册。
let unregisterOnline: (() => void) | null = null
const convRef = ref<HTMLElement | null>(null)
const artifacts = ref<Artifact[]>([])

// M9d: 预览面板宽度 — 按比例随屏幕自适应 + 可拖拽控制。比例持久化在 sessionStorage。
const PREVIEW_RATIO_KEY = 'seedai:preview-ratio'
const previewRatio = ref<number>(Number(sessionStorage.getItem(PREVIEW_RATIO_KEY)) || 42)
const resizing = ref(false)
const chatRootRef = ref<HTMLElement | null>(null)

// M9d: 拖拽交互。核心难点: 预览区是 <iframe>, 指针一旦移到 iframe 上方,
// 手柄自身的 pointermove 就收不到(指针不在手柄上)。解法:
// (1) 拖拽期间把 pointermove/pointerup 挂到 window —— 无论指针在哪都收到;
// (2) setPointerCapture 双保险 —— 跨 iframe 时事件仍被锁定回手柄(会冒泡到 window)。
// 两者结合保证「面板实时跟随鼠标, 松手停在当前位置」。Pointer 事件统一鼠标/触摸/笔。
function startResize(e: PointerEvent) {
  e.preventDefault()
  resizing.value = true
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
  // 挂到 window: 指针移出手柄/进入 iframe 也能持续收到 move。
  window.addEventListener('pointermove', onResizeMove)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
  try {
    ;(e.currentTarget as Element).setPointerCapture(e.pointerId)
  } catch {
    /* 无 capture 时退化为 window listener, 仍可用 */
  }
}
function onResizeMove(e: PointerEvent) {
  if (!resizing.value || !chatRootRef.value) return
  const rect = chatRootRef.value.getBoundingClientRect()
  // preview-panel 在右侧, 它本身宽度 = (100 - 手柄左侧占比)%。
  // 若直接把 cursorX 占比当作 previewRatio, 手柄会"反着跑"(往左移面板往右胀)。
  // 故取右补集: 手柄左侧占 f → 右侧预览占比 = 100 - f, 手柄才真正跟随指针。
  const f = (e.clientX - rect.left) / rect.width
  let pct = (1 - f) * 100
  pct = Math.min(72, Math.max(24, pct)) // 限制区间, 保证两栏都可用
  previewRatio.value = pct
}
function stopResize() {
  if (!resizing.value) return
  resizing.value = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  window.removeEventListener('pointermove', onResizeMove)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
  sessionStorage.setItem(PREVIEW_RATIO_KEY, String(Math.round(previewRatio.value)))
}
const publishedUrl = computed<string | null>(
  () => projectStore.projects.find((p) => p.id === projectStore.currentProjectId)?.published_url ?? null,
)

// 建站叙事判据：优先以「后端下发的本轮真实意图」为准——done 事件里带 intents，
// 含 site 域才显示"构建网站/生成预览"等建站文案；否则（chat/research/project）一律走中性文案。
// 杜绝"复用已存在的建站 project 后，闲聊也被说成网站建设中"的近似误判。
// 生成早期（intents 尚未随 done 下发）一律按中性处理，不提前误报"建设中"，避免文案抖动。
const isSiteBuild = computed(() => {
  if (stream.intents && stream.intents.length > 0) {
    return stream.intents.some((i) => i.domain === 'site')
  }
  // 尚未拿到真实意图：仅当非生成态（历史已完成的轮，且当时没有 site 意图）才由 project 兜底，
  // 生成中同样按中性，防止闲聊在 project 内显示建设流程。
  return !generating.value && projectStore.currentProjectId != null
})

// 执行进度面板(ThinkingTrail/ApprovalCard 等)是否显示。
// 仅当「正在生成 / 有待审批 / 已暂停 / 出错」时展示；done 终态后(generating=false 且无上述卡片)
// ThinkingTrail 自动折叠为"查看思考过程"入口，不再占用版面(用户诉求: 最终结果出来后隐藏)。
// 注意: 早期用 stream.lastSeq>0 作为判据, 导致 done 后 lastSeq 仍 >0 → 面板残留不隐藏, 现已移除。
const hasLivePanel = computed(() =>
  generating.value
  || !!stream.approval
  || !!stream.suspended
  || !!stream.error,
)

const streamErrorText = computed(() => readText(stream.error, ['message', 'detail', 'code']))
const suspendedText = computed(() => readText(stream.suspended, ['message', 'reason', 'status']) || '本轮已暂停，等待下一步操作。')

function createClientMessageId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `msg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
}

async function ensureConversation(message: string): Promise<number> {
  let projectId = projectStore.currentProjectId
  if (projectId == null) {
    const project = await createProject(message.slice(0, 24) || '新项目')
    await projectStore.load()
    projectStore.currentProjectId = project.id
    projectId = project.id
  }
  if (convStore.currentConvId == null) {
    await convStore.create(projectId, message.slice(0, 24) || '新对话')
  }
  return convStore.currentConvId!
}

async function performSend(message: string, clientMsgId: string): Promise<void> {
  errorMessage.value = ''
  offlineNote.value = ''
  try {
    const conversationId = await ensureConversation(message)
    const userMessage = optimisticMessage('user', message, conversationId)
    const assistantMessage = optimisticMessage('assistant', '', conversationId)
    convStore.messages.push(userMessage, assistantMessage)
    activeAssistant.value = assistantMessage
    input.value = ''
    // 每轮新对话先清空上一轮的阶段/活动/序列号状态，否则 stream.lastSeq 沿用上轮尾值，
    // 新流事件 seq=1 <= lastSeq 会被 reducer 直接丢弃(StageRail 卡在上轮数据、进度条看似不动)。
    resetStreamUiState(stream)
    generating.value = true

    subscription.value?.abort()
    const sub = startChat(
      { client_msg_id: clientMsgId, conversation_id: conversationId, message },
      streamHandlers,
    )
    subscription.value = sub
    await sub.finished
    scrollToBottom()
  } catch (error) {
    generating.value = false
    errorMessage.value = error instanceof Error ? error.message : '无法创建对话'
  }
}

async function send(clientMsgId?: string): Promise<void> {
  const message = input.value.trim()
  if (!message || generating.value) return
  if (!auth.user.value) {
    auth.openLogin()
    return
  }

  // 离线: 先持久化, 联网后由 offlineQueue 串行幂等补发(后端按 client_msg_id 去重)。
  if (!isOnline()) {
    const id = clientMsgId || createClientMessageId()
    await offlineQueue.enqueue({ client_msg_id: id, message, conversation_id: projectStore.currentProjectId })
    offlineNote.value = '离线：消息已存入本地队列，联网后自动发送'
    input.value = ''
    return
  }

  await performSend(message, clientMsgId || createClientMessageId())
}

// 离线队列补发: 逐条串行执行(后端按 client_msg_id 幂等去重, 重复提交只重挂接已有流)。
// 重入锁: 本函数在 onMounted(在线且队列非空) 与 onOnline 回调两处都会触发。两者可能
// 在同一时刻并发执行(onMounted 刚跑完判断, 紧接着 window 触发 online), 各自读到同一批 pending
// 条目 → 各调一次 performSend → 同一条消息被生成两次(用户侧表现为"发送一次却出现 2 条回复")。
// 故用 _flushing 互斥, 保证任意时刻只有一路补发在进行。
let _flushing = false
async function flushOfflineQueue(): Promise<void> {
  if (_flushing) return
  if (generating.value) return
  if ((await offlineQueue.count()) === 0) return
  _flushing = true
  try {
    offlineNote.value = '正在补发离线消息…'
    const sent = await offlineQueue.flush(async (item: QueuedMessage) => {
      await performSend(item.message, item.client_msg_id)
    })
    offlineNote.value = sent > 0 ? `已补发 ${sent} 条离线消息` : ''
  } catch {
    offlineNote.value = '离线消息补发失败，联网后将自动重试'
  } finally {
    _flushing = false
  }
  setTimeout(() => {
    if (offlineNote.value.startsWith('已补发') || offlineNote.value.startsWith('正在补发')) {
      offlineNote.value = ''
    }
  }, 4000)
}

const streamHandlers = {
  onEvent(event: StreamEvent) {
    const result = reduceStreamEvent(stream, event)
    if (!result.applied) return

    if (activeAssistant.value) {
      activeAssistant.value.content = stream.response
      // 把本轮回放的 trace_id 挂到消息上, 供气泡内「评价」提交 feedbacks 关联。
      if (stream.traceId) activeAssistant.value.trace_id = stream.traceId
    }
    saveResumeRef()
    if (result.gapAfter !== null && stream.streamId) void recoverGap(result.gapAfter)

    if (event.type === 'error') {
      generating.value = false
      stopping.value = false
      errorMessage.value = streamErrorText.value || '本轮执行失败'
      clearResumeRef()
      void reconcileTerminal()
    } else if (event.type === 'done') {
      generating.value = false
      stopping.value = false
      clearResumeRef()
      void reconcileTerminal()
    } else if (event.type === 'suspended') {
      generating.value = false
      stopping.value = false
    } else if (event.type === 'reconnect') {
      // 断连重订阅后向 Turn API 请求快照对账, 修正本地可能因乱序/丢失而偏离的状态。
      void reconcileTerminal()
      if (!stream.approval) void restorePendingApproval()
    }
    scrollToBottom()
  },
  onError(error: Error) {
    if (replaying.value) return
    generating.value = false
    stopping.value = false
    errorMessage.value = error.message
  },
}

async function recoverGap(after: number): Promise<void> {
  if (!stream.streamId || replaying.value) return
  replaying.value = true
  replaySubscription.value?.abort()
  replaySubscription.value = replayStream(stream.streamId, after, {
    onEvent: streamHandlers.onEvent,
    onError: (error) => { errorMessage.value = error.message },
  })
  await replaySubscription.value.finished
  replaying.value = false
}

async function replaySavedStream(): Promise<void> {
  const resume = loadResumeRef()
  if (!resume || generating.value) return
  resetStreamUiState(stream)
  generating.value = true
  const lastMessage = [...convStore.messages].reverse().find((message) => message.role === 'assistant')
  if (lastMessage) activeAssistant.value = lastMessage
  replaySubscription.value?.abort()
  replaySubscription.value = replayStream(resume.streamId, resume.after, streamHandlers)
  await replaySubscription.value.finished
}

async function stop(): Promise<void> {
  if (!stream.turnId || stopping.value) return
  stopping.value = true
  try {
    await controlTurn(stream.turnId, 'stop')
  } catch (error) {
    stopping.value = false
    errorMessage.value = error instanceof Error ? error.message : '无法停止任务'
  }
}

const APPROVAL_TERMINAL = new Set(['approved', 'rejected', 'expired', 'invalidated', 'consumed', 'submitted'])

async function decideApproval(decision: 'approve' | 'reject'): Promise<void> {
  const approvalId = readText(stream.approval, ['approval_id'])
  // 双段确认复用同一一次性 nonce(后端两张 approve 都校验 challenge_nonce_hash);
  // reducer 已在不带 nonce 的 pending_second 事件中保留它。
  const decisionNonce = readText(stream.approval, ['decision_nonce', 'decision_nonce_2', 'challenge_nonce'])
  if (!approvalId || !decisionNonce) {
    errorMessage.value = '审批事件缺少 approval_id 或 decision_nonce'
    return
  }
  approvalSubmitting.value = true
  errorMessage.value = ''
  try {
    await submitApproval(approvalId, decision, decisionNonce)
    // 终态由 SSE 事件权威驱动: 拒绝直接标记终态; 批准交还 SSE(pending_second / done)。
    if (decision === 'reject' && stream.approval) {
      stream.approval = { ...stream.approval, status: 'rejected' }
    }
  } catch (error) {
    const msg = error instanceof Error ? error.message : '提交审批决定失败'
    errorMessage.value = msg
    // 409 过期/已消费/非待决 -> 同步卡片状态, 便于用户重新发起审批。
    const upper = msg.toUpperCase()
    if (stream.approval) {
      if (upper.includes('EXPIRED')) stream.approval = { ...stream.approval, status: 'expired' }
      else if (upper.includes('CONSUMED') || upper.includes('NOT_PENDING') || upper.includes('INVALID')) {
        stream.approval = { ...stream.approval, status: 'invalidated' }
      }
    }
  } finally {
    approvalSubmitting.value = false
  }
}

// 气泡内「评价」提交: 调后端 POST /api/feedback, 关联 trace_id + conversation_id。
async function onRate(payload: {
  rating: number
  comment: string
  dimensions: Record<string, number>
  traceId?: string | null
  conversationId?: number | null
}): Promise<void> {
  if (!payload.traceId) {
    errorMessage.value = '缺少 trace_id，无法提交评价'
    return
  }
  try {
    await submitFeedback({
      traceId: payload.traceId,
      rating: payload.rating,
      comment: payload.comment,
      dimensions: payload.dimensions,
      conversationId: payload.conversationId ?? convStore.currentConvId,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交评价失败'
  }
}

// 倒计时归零时由 ApprovalCard 抛出, 同步本地卡片为过期态(避免误提交)。
function onApprovalExpired(): void {
  if (stream.approval && !APPROVAL_TERMINAL.has(String(stream.approval.status))) {
    stream.approval = { ...stream.approval, status: 'expired' }
  }
}

async function reconcileTerminal(): Promise<void> {
  if (!stream.turnId) return
  try {
    const turn = await getTurn(stream.turnId)
    const response = typeof turn.response === 'string'
      ? turn.response
      : typeof turn.final_response === 'string'
        ? turn.final_response
        : ''
    if (response && activeAssistant.value) activeAssistant.value.content = response
  } catch {
    // 终态对账失败不覆盖已收到的可靠流内容。
  }
}

async function loadArtifacts(): Promise<void> {
  if (projectStore.currentProjectId == null) {
    artifacts.value = []
    return
  }
  try {
    artifacts.value = await listArtifacts(projectStore.currentProjectId)
  } catch {
    artifacts.value = []
  }
}

async function restorePendingApproval(): Promise<void> {
  try {
    const pending = await getPendingApprovals()
    const approval = Array.isArray(pending) ? pending[0] : pending.approvals?.[0]
    if (approval && typeof approval === 'object' && !Array.isArray(approval)) stream.approval = approval as Record<string, unknown>
  } catch {
    // 断网或没有待审批项时不影响正常聊天。
  }
}

function optimisticMessage(role: 'user' | 'assistant', content: string, conversationId: number): Message {
  return {
    id: 0,
    conversation_id: conversationId,
    role,
    content,
    model_id: role === 'assistant' ? model.value : null,
    created_at: new Date().toISOString(),
  }
}

// 发布弹窗确认: 把编辑的 text + 勾选文件清单组装成一条 chat 消息发后端,
// 复用现有 publish 链路(意图命中 PUBLISH_WORDS → 发布审批卡 → site_deploy)。
// 文件清单以 [PUBLISH_FILES]...[/PUBLISH_FILES] 结构化令牌内嵌, 后端 S5 解析后做增量发布。
async function onPublish(payload: { text: string; files: string[] }) {
  if (!auth.user.value) {
    auth.openLogin()
    return
  }
  const text = payload.text.trim() || `发布当前网站版本`
  const fileBlock = payload.files.length
    ? `\n[PUBLISH_FILES]\n${payload.files.join('\n')}\n[/PUBLISH_FILES]`
    : ''
  const message = `${text}${fileBlock}`
  await performSend(message, createClientMessageId())
}

function scrollToBottom(): void {
  void nextTick(() => {
    if (convRef.value) convRef.value.scrollTop = convRef.value.scrollHeight
  })
}

function saveResumeRef(): void {
  if (!stream.streamId || !stream.turnId) return
  const resume: ResumeRef = { streamId: stream.streamId, turnId: stream.turnId, after: stream.lastSeq }
  sessionStorage.setItem(RESUME_KEY, JSON.stringify(resume))
}

function loadResumeRef(): ResumeRef | null {
  try {
    const raw = sessionStorage.getItem(RESUME_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as ResumeRef
    return typeof value.streamId === 'string' && typeof value.turnId === 'string' && typeof value.after === 'number' ? value : null
  } catch {
    return null
  }
}

function clearResumeRef(): void {
  sessionStorage.removeItem(RESUME_KEY)
}

function readText(source: Record<string, unknown> | null, keys: string[]): string {
  if (!source) return ''
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'string' && value) return value
  }
  return ''
}

onMounted(async () => {
  await auth.init()
  if (!auth.user.value) return
  await projectStore.load()
  if (projectStore.currentProjectId != null) await convStore.loadConversations(projectStore.currentProjectId)
  await loadArtifacts()
  await restorePendingApproval()
  await replaySavedStream()
  scrollToBottom()
  // M9c: 联网恢复时按 client_msg_id 串行、幂等补发离线队列。
  unregisterOnline = onOnline(() => void flushOfflineQueue())
  // 若挂载时已在线但存在上次离线遗留的待发消息(如离线期间刷新了页面), 立即补发。
  if (isOnline() && (await offlineQueue.count()) > 0) void flushOfflineQueue()
})

onUnmounted(() => {
  subscription.value?.abort()
  replaySubscription.value?.abort()
  unregisterOnline?.()
})

watch(() => projectStore.currentProjectId, async (projectId) => {
  if (projectId == null) return
  await convStore.loadConversations(projectId)
  await loadArtifacts()
  activeAssistant.value = null
  scrollToBottom()
})

watch(() => stream.response, scrollToBottom)
</script>

<template>
  <div class="chat" ref="chatRootRef" :class="{ resizing }">
    <section class="thread-panel">
      <header class="chat-header">
        <div>
          <span class="eyebrow">{{ projectStore.currentProjectId ? '当前项目' : '新对话' }}</span>
          <h1>{{ projectStore.projects.find((project) => project.id === projectStore.currentProjectId)?.name || '开始构建' }}</h1>
        </div>
        <span v-if="stream.turnId" class="turn-id">Turn {{ stream.turnId.slice(0, 8) }}</span>
      </header>

      <main ref="convRef" class="conversation">
        <div v-if="!convStore.messages.length" class="empty-state">
          描述你的目标；系统会在执行过程中展示阶段、任务、工具和审批状态。
        </div>
        <MessageBubble
          v-for="(message, index) in convStore.messages"
          :key="`${message.id}-${index}`"
          :role="message.role"
          :content="message.content"
          :time="message.created_at"
          :streaming="message === activeAssistant && generating"
          :thinking="message === activeAssistant && generating ? stream.thinking : ''"
          :trace-id="message.trace_id"
          :conversation-id="convStore.currentConvId"
          :can-rate="!!auth.user.value && message.role === 'assistant'"
          @rate="onRate"
        >
          <template v-if="message === activeAssistant && hasLivePanel" #trail>
            <!-- 合并后的思考/执行流：生成中逐段追加展示；done 后折叠为「查看思考过程」可回看(A方案)。
                 按 isSiteBuild 切换建站叙事 vs 中性文案，闲聊不再显示"网站建设中"。 -->
            <ThinkingTrail
              :stages="stream.stages"
              :activities="stream.activities"
              :thinking="stream.thinking"
              :capability-notices="stream.capabilityNotices"
              :usage="stream.usage"
              :generating="generating"
              :is-site-build="isSiteBuild"
              :show-development="devMode"
            />
            <div v-if="stream.attemptOutputs.length" class="attempt-output">
              <b>本次尝试输出</b>
              <p v-for="(output, outputIndex) in stream.attemptOutputs" :key="outputIndex">{{ output }}</p>
            </div>
            <ApprovalCard
              v-if="stream.approval"
              :approval="stream.approval"
              :submitting="approvalSubmitting"
              @decision="decideApproval"
              @expired="onApprovalExpired"
              @reauth="auth.openLogin()"
            />
            <div v-if="stream.suspended" class="suspended">{{ suspendedText }}</div>
          </template>
        </MessageBubble>
      </main>

      <footer class="composer">
        <p v-if="replaying" class="status-line">正在补齐缺失的流事件…</p>
        <p v-if="offlineNote" class="offline-line">{{ offlineNote }}</p>
        <p v-if="errorMessage || streamErrorText" class="error-line">{{ errorMessage || streamErrorText }}</p>
        <ChatInput
          v-model:value="input"
          v-model:model="model"
          :generating="generating"
          :cancelling="stopping"
          :models="models"
          @send="send"
          @stop="stop"
        />
      </footer>
    </section>

    <div
      class="resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整预览宽度"
      title="拖动调整预览宽度"
      @pointerdown="startResize"
    ></div>

    <aside class="preview-panel" :style="{ width: previewRatio + '%' }">
      <RightPanel
        :artifacts="artifacts"
        :generating="generating"
        :project-id="projectStore.currentProjectId"
        :published-url="publishedUrl"
        @refresh="loadArtifacts"
        @publish="onPublish"
      />
    </aside>
  </div>
</template>

<style scoped>
.chat { display: flex; flex: 1; min-width: 0; min-height: 0; background: var(--surface-1); }
.thread-panel { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; }
.chat-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 18px; border-bottom: 1px solid var(--border); background: var(--panel); }
.eyebrow { display: block; color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
h1 { margin: 3px 0 0; color: var(--text); font-size: 16px; }
.turn-id { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.conversation { display: flex; flex: 1; flex-direction: column; gap: 10px; overflow-y: auto; padding: 18px; }
.empty-state { max-width: 540px; margin: auto; border: 1px dashed var(--border); border-radius: 14px; padding: 20px; color: var(--muted); font-size: 13px; line-height: 1.7; text-align: center; }
.composer { border-top: 1px solid var(--border); padding: 12px 16px; background: var(--panel); }
.status-line, .error-line { margin: 0 0 8px; border-radius: 8px; padding: 7px 10px; font-size: 12px; }
.status-line { background: var(--brand-bg); color: var(--brand); }
.offline-line { background: var(--warn-bg); color: var(--warn); }
.error-line { background: var(--err-bg); color: var(--err); }
.attempt-output, .suspended { margin: 10px 0; border-radius: 10px; padding: 10px 12px; font-size: 12px; line-height: 1.6; }
.attempt-output { border: 1px solid var(--border); background: var(--surface-2); color: var(--text-3); }
.attempt-output b { color: var(--text); }
.attempt-output p { margin: 6px 0 0; white-space: pre-wrap; }
.suspended { border: 1px solid var(--warn-border); background: var(--warn-bg); color: var(--warn); }
.preview-panel { flex: 0 0 auto; min-width: 280px; max-width: 1100px; border-left: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; }
.resize-handle {
  flex: 0 0 8px;
  cursor: col-resize;
  background: transparent;
  position: relative;
  /* 触摸拖动时禁用浏览器默认手势(滚动/缩放), 让 pointermove 稳定触发 */
  touch-action: none;
  transition: background 0.15s;
}
.resize-handle::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  border-radius: 2px;
  background: var(--border);
}
.resize-handle:hover::after,
.chat.resizing .resize-handle::after { background: var(--brand); width: 3px; left: 2px; }
.resize-handle:hover,
.chat.resizing .resize-handle { background: var(--brand-bg); }
/* 拖拽中禁用预览 iframe 的指针事件, 双保险确保跨 iframe 拖动不丢失 */
.chat.resizing :deep(iframe) { pointer-events: none; }
/* 窄屏(手机): 预览占满, 对话栏隐藏, 保证预览可用且高度撑满。 */
@media (max-width: 720px) {
  .thread-panel { display: none; }
  .resize-handle { display: none; }
  .preview-panel { width: 100% !important; min-width: 0; max-width: none; }
}
</style>
