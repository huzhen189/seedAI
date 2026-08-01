<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import ActivityPanel from '../components/ActivityPanel.vue'
import ApprovalCard from '../components/ApprovalCard.vue'
import ChatInput from '../components/ChatInput.vue'
import MessageBubble from '../components/MessageBubble.vue'
import RightPanel from '../components/RightPanel.vue'
import StageRail from '../components/StageRail.vue'
import {
  controlTurn,
  getPendingApprovals,
  getTurn,
  replayStream,
  startChat,
  submitApproval,
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

const hasLivePanel = computed(() =>
  generating.value
  || stream.lastSeq > 0
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
    resetStreamUiState(stream)
    const userMessage = optimisticMessage('user', message, conversationId)
    const assistantMessage = optimisticMessage('assistant', '', conversationId)
    convStore.messages.push(userMessage, assistantMessage)
    activeAssistant.value = assistantMessage
    input.value = ''
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
async function flushOfflineQueue(): Promise<void> {
  if (generating.value) return
  if ((await offlineQueue.count()) === 0) return
  offlineNote.value = '正在补发离线消息…'
  try {
    const sent = await offlineQueue.flush(async (item: QueuedMessage) => {
      await performSend(item.message, item.client_msg_id)
    })
    offlineNote.value = sent > 0 ? `已补发 ${sent} 条离线消息` : ''
  } catch {
    offlineNote.value = '离线消息补发失败，联网后将自动重试'
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

    if (activeAssistant.value) activeAssistant.value.content = stream.response
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

async function decideApproval(decision: 'approve' | 'reject'): Promise<void> {
  const approvalId = readText(stream.approval, ['approval_id'])
  const decisionNonce = readText(stream.approval, ['decision_nonce', 'challenge_nonce'])
  if (!approvalId || !decisionNonce) {
    errorMessage.value = '审批事件缺少 approval_id 或 decision_nonce'
    return
  }
  approvalSubmitting.value = true
  try {
    await submitApproval(approvalId, decision, decisionNonce)
    if (stream.approval) stream.approval = { ...stream.approval, status: decision === 'approve' ? 'submitted' : 'rejected' }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交审批决定失败'
  } finally {
    approvalSubmitting.value = false
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
  <div class="chat">
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
          :can-rate="false"
        >
          <template v-if="message === activeAssistant && hasLivePanel" #trail>
            <StageRail :stages="stream.stages" :show-development="devMode" />
            <ActivityPanel
              :activities="stream.activities"
              :capability-notices="stream.capabilityNotices"
              :usage="stream.usage"
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

    <aside class="preview-panel">
      <RightPanel
        :artifacts="artifacts"
        :generating="generating"
        :project-id="projectStore.currentProjectId"
        @refresh="loadArtifacts"
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
.preview-panel { width: min(42%, 560px); min-width: 300px; border-left: 1px solid var(--border); }
@media (max-width: 900px) { .preview-panel { display: none; } }
</style>
