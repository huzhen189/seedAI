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
import { computed, onMounted, onUnmounted, ref, reactive, watch, nextTick } from 'vue'
import ThoughtTrail from '../components/ThoughtTrail.vue'
import RightPanel from '../components/RightPanel.vue'
import ChatInput from '../components/ChatInput.vue'
import MessageBubble from '../components/MessageBubble.vue'
import SubTaskTrack from '../components/SubTaskTrack.vue'
import MergedResult from '../components/MergedResult.vue'
import { startChat, cancelChat, fetchModels, sendFeedback, type ChatCallbacks } from '../api/chat'
import { listArtifacts, renameProject, patch } from '../api/projects'
import { useAuth } from '../composables/useAuth'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import type { Artifact, Message, ModelInfo, OptionEvent, AlternativesEvent, PlanEvent, RetryEvent, ThoughtStep, BlockEvent, ConfirmEvent, QcResult, RatingDims, SubTaskView, OrchestrationEvent, SubTaskStartEvent, SubTaskDoneEvent, SubTaskFailEvent, MergeEvent, FailedSubTask } from '../types'

const STAGE_LABELS: Record<string, string> = {
  enter_router: '意图路由 — 识别你的需求类型，匹配最合适的处理流程',
  dispatch: '技能调度 — 加载所需的 AI 能力和工具链',
  enter_planner: '需求规划 — 拆解任务、制定执行步骤和产出目标',
  enter_coder: '代码生成 — 正在为你编写/构建代码',
  enter_reviewer: '评审校验 — 检查生成结果的完整性和正确性',
  previewing: '投递预览 — 将生成产物上传到预览环境',
  preview: '生成预览 — 正在生成可预览的网页',
  done: '完成 — 全部任务执行完毕',
}

// ---- 本地 UI 状态 ----
const models = ref<ModelInfo[]>([])
const model = ref('deepseek')
const input = ref('')
const generating = ref(false)
const finished = ref(false)

// 思考时间线(每步一个 agent 节点)+ 计划特殊节点;替代旧版混成一坨的 thinks 字符串。
const thoughtSteps = ref<ThoughtStep[]>([])
const planNodes = ref<PlanEvent[]>([])
const currentStage = ref('')
const degraded = ref(false)
const generatedHtml = ref('')
const requirementDoc = ref<Record<string, any> | null>(null)
const previewUrl = ref<string | null>(null)
const errorMsg = ref('')

// 断点续跑(§7): 检测到 status=paused 的会话
const pausedConv = computed(() =>
  convStore.conversations.find(c => c.status === 'paused'),
)

// 消息队列: 生成中用户输入排队, 完成后自动发下一条
const msgQueue = ref<{ text: string; editing: boolean }[]>([])
const queueVisible = ref(false)

function enqueue(text: string) {
  msgQueue.value.push({ text, editing: false })
  queueVisible.value = true
  input.value = ''
}

function dequeueAndSend() {
  const next = msgQueue.value.shift()
  if (!next) { queueVisible.value = false; return }
  input.value = next.text
  doSend(next.text)
}

function editQueueItem(idx: number) {
  const item = msgQueue.value[idx]
  if (!item) return
  item.editing = true
}

function saveQueueItem(idx: number, newText: string) {
  const item = msgQueue.value[idx]
  if (!item || !newText.trim()) return
  item.text = newText.trim()
  item.editing = false
}

function deleteQueueItem(idx: number) {
  msgQueue.value.splice(idx, 1)
  if (msgQueue.value.length === 0) queueVisible.value = false
}

function sendNowQueueItem(idx: number) {
  const item = msgQueue.value[idx]
  if (!item) return
  msgQueue.value.splice(idx, 1)
  input.value = item.text
  doSend(item.text)
}

async function resumeConversation() {
  const pc = pausedConv.value
  if (!pc || !projectStore.currentProjectId) return
  resetGenState()
  traceId.value = genTraceId()
  convStore.currentConvId = pc.id
  setActiveGen(pc.id, traceId.value)
  generating.value = true
  startChat({ model: model.value, traceId: traceId.value, conversationId: pc.id, cb: makeCallbacks(0), resume: true })
}

async function abortPaused() {
  const pc = pausedConv.value
  if (!pc) return
  await patch(`/api/conversations/${pc.id}`, { status: 'aborted', checkpoint_data: null })
  // 刷新会话列表
  if (projectStore.currentProjectId) await convStore.loadConversations(projectStore.currentProjectId)
}

// 意图识别(由 AI intent 事件设置; 控制右侧面板显示)
const currentIntent = ref<{ level1: string; level2: string }>({ level1: '', level2: '' })
const rightCollapsed = ref(false)
// 方案确认(改文字回复驱动, 不再弹模态框): Planner 产出后暂停等待用户以文字回复续跑
const awaitingConfirm = ref(false)
const awaitPlan = ref<{ title: string; goal: string; steps: string[] } | null>(null)

// 方案选择(options 事件): 前端弹出单选框, 选中后记录, 下次 send 时一起发送
const showOptionsModal = ref(false)
const optionsData = ref<OptionEvent | null>(null)
const selectedOption = ref('')  // radio 单选绑定
const pendingOptionsText = ref('')  // 已确认但未发送的选项文本
// 非阻塞候选提示(管道级 alternatives 事件): 系统已自行决定 top-1, 列出可切换候选
const alternativesData = ref<AlternativesEvent | null>(null)

// 把用户输入解析为候选项索引(A-H / 1-9 / 中文数字 / 选X / 用X / 切换X / 第X个);
// 不是选择则返 null。对齐后端 intent/selection.parse_selection。
const _SELECT_RE = /^\s*(?:([A-Ha-h])\s*|([1-9])\s*|([一二两三四五六七八九十])\s*|(?:选|用|切换|改成|改为)\s*([A-Ha-h1-9一二两三四五六七八九十])\s*|第\s*([一二两三四五六七八九十])\s*个)\s*$/
const _CN_NUM: Record<string, number> = { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }
function parseSelectionToken(text: string): number | null {
  const m = _SELECT_RE.exec(text.trim())
  if (!m) return null
  const [, letter, digit, cn, pick, nth] = m
  if (letter) return letter.toUpperCase().charCodeAt(0) - 65
  if (digit) return parseInt(digit, 10) - 1
  if (cn) return _CN_NUM[cn] - 1
  if (pick) return /[A-Ha-h]/.test(pick) ? pick.toUpperCase().charCodeAt(0) - 65 : _CN_NUM[pick] - 1
  if (nth) return _CN_NUM[nth] - 1
  return null
}
function clearAlternatives() {
  alternativesData.value = null
}
// 二次确认弹框(安全 high)
const pendingConfirm = ref<{ reason: string; skill: string } | null>(null)
const showConfirmModal = ref(false)
// 高危拦截提示(安全 critical, 不可绕过)
const blockReason = ref('')

function onOptionsConfirm() {
  if (!optionsData.value || !selectedOption.value) return
  const choices = optionsData.value.choices || []
  const selected = choices.find(c => c.id === selectedOption.value)
  if (!selected) return
  // 管道级多选项: 选中即带 skill 参数重发(不拼文本)
  if (optionsData.value.mode === 'skill') {
    showOptionsModal.value = false
    resendWithSkill(selected.id)
    return
  }
  // requirement_agent 级: 拼文本, 下次 send 一起发
  pendingOptionsText.value = `方案确认: 选择了 ${selected.id}: ${selected.title}`
  showOptionsModal.value = false
  upsertStep('option_selected', 'done', `已选择: ${selected.id}. ${selected.title}`)
  // 用户没在打字 → 自动发送选项
  if (!input.value.trim()) {
    sendOptionsNow()
  }
}

async function sendOptionsNow() {
  if (!pendingOptionsText.value) return
  const text = pendingOptionsText.value
  pendingOptionsText.value = ''
  input.value = text
  await send()
}

function cancelOptions() {
  showOptionsModal.value = false
  selectedOption.value = ''
  optionsData.value = null
}

// 文字回复驱动续跑: 复用当前 trace_id, 把用户文字作为改进意见/确认一并带入后端
// (业务代理会把 q 追加到 checkpoint messages, 无需再弹确认模态框)。
function resumeFromPlan(text: string) {
  awaitingConfirm.value = false
  resetGenState()
  generating.value = true
  const cid = convStore.currentConvId!
  setActiveGen(cid, traceId.value)
  // 本地乐观追加用户回复 + 空 assistant 占位(后端以 checkpoint messages 为准)
  convStore.messages.push({
    role: 'user', content: text, conversation_id: cid, id: 0, created_at: '', model_id: model.value,
  } as any)
  convStore.messages.push({
    role: 'assistant', content: '', conversation_id: cid, id: 0, created_at: '', model_id: model.value,
  } as any)
  const assistantIdx = convStore.messages.length - 1
  lastSentText.value = text
  esRef.value = startChat({
    model: model.value,
    traceId: traceId.value,
    conversationId: cid,
    resume: true,
    q: text,
    cb: makeCallbacks(assistantIdx),
  })
}

// 二次确认通过后: 带 confirmed 标记重发(Worker 据此跳过安全拦截, 执行 selected_skill)
function resendConfirmed() {
  if (!pendingConfirm.value) return
  pendingConfirm.value = null
  showConfirmModal.value = false
  resetGenState()
  traceId.value = genTraceId()
  const cid = convStore.currentConvId!
  setActiveGen(cid, traceId.value)
  generating.value = true
  const assistantIdx = convStore.messages.length - 1
  esRef.value = startChat({
    model: model.value,
    traceId: traceId.value,
    conversationId: cid,
    confirmed: true,
    cb: makeCallbacks(assistantIdx),
  })
}

// 多选项选中: 带 skill 参数重发(Worker 直接执行该 skill)
function resendWithSkill(skillName: string) {
  resetGenState()
  traceId.value = genTraceId()
  const cid = convStore.currentConvId!
  setActiveGen(cid, traceId.value)
  generating.value = true
  const assistantIdx = convStore.messages.length - 1
  esRef.value = startChat({
    model: model.value,
    traceId: traceId.value,
    conversationId: cid,
    skill: skillName,
    cb: makeCallbacks(assistantIdx),
  })
}

// 多意图中风险子任务确认后重发:把该 id 加入已确认集合,以同一原始请求重跑(让 MEDIUM 子任务放行)。
function confirmSubTask(subTaskId: string) {
  if (!confirmedSubtaskIds.value.includes(subTaskId)) confirmedSubtaskIds.value.push(subTaskId)
  // 清空上一轮 assistant 占位内容, 避免合并结果重复累积
  const idx = findAssistantIdx()
  if (idx >= 0) convStore.messages[idx].content = ''
  resetGenState()
  traceId.value = genTraceId()
  const cid = convStore.currentConvId!
  setActiveGen(cid, traceId.value)
  generating.value = true
  esRef.value = startChat({
    model: model.value,
    traceId: traceId.value,
    conversationId: cid,
    confirmedSubtasks: confirmedSubtaskIds.value,
    cb: makeCallbacks(idx),
  })
}

// ---- 上翻加载更早会话 ----
const convRef = ref<HTMLElement | null>(null)
const sentinel = ref<HTMLElement | null>(null)
let scrollObserver: IntersectionObserver | null = null

function setupScrollLoading() {
  if (!sentinel.value) return
  scrollObserver = new IntersectionObserver(
    async (entries) => {
      if (entries[0].isIntersecting && !convStore.loadingMore) {
        const scroller = convRef.value
        const prevHeight = scroller?.scrollHeight || 0
        const hasMore = await convStore.loadMoreHistory()
        if (hasMore && scroller) {
          await nextTick()
          scroller.scrollTop = scroller.scrollHeight - prevHeight
        }
      }
    },
    { threshold: 0.1 },
  )
  scrollObserver.observe(sentinel.value)
}
function teardownScrollLoading() {
  scrollObserver?.disconnect()
  scrollObserver = null
}

// ---- 自动滚动到底部(微信风格) ----
let autoScroll = true  // 用户手动上滚后暂停自动滚动
function scrollToBottom(smooth = true) {
  const el = convRef.value
  if (!el) return
  nextTick(() => {
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'instant' })
  })
}
function onConvScroll() {
  const el = convRef.value
  if (!el) return
  // 距底部 > 80px 视为用户手动上滚, 暂停自动追底
  autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

async function loadArtifacts() {
  const pid = projectStore.currentProjectId
  if (pid == null) {
    projectArtifacts.value = []
    return
  }
  try {
    projectArtifacts.value = await listArtifacts(pid)
  } catch {
    projectArtifacts.value = []
  }
}
const traceId = ref('')
const esRef = ref<EventSource | null>(null)
// 右侧预览面板实例(ChatView 通过它联动选中文件并打开预览)
const rightPanel = ref<InstanceType<typeof RightPanel> | null>(null)
// 后置 QC 三裁判结果 / 用户评价, 均按 trace_id 索引(气泡内展示, v0.8.5 M1)
const qcMap = reactive<Record<string, QcResult>>({})
const ratedMap = reactive<Record<string, { rating: number; dims: RatingDims; comment: string }>>({})
const projectArtifacts = ref<Artifact[]>([])

// ---- 文字产物链接(替代确认模态框): 需求文档 + 所有生成文件, 点击联动右侧预览 ----
function iconForFile(name: string) {
  const map: Record<string, string> = {
    html: '🌐', css: '🎨', js: '⚡', json: '📋', svg: '🖼️',
    png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', webp: '🖼️',
    md: '📝', txt: '📄', py: '🐍', ts: '🔷', zip: '📦',
  }
  const i = name.lastIndexOf('.')
  const e = i > 0 ? name.slice(i + 1).toLowerCase() : ''
  return map[e] || '📄'
}

// 与右侧 req-tree / file-tree 保持一致的产物清单(需求文档 + 全部文件)
const artifactLinks = computed(() => {
  const links: { name: string; icon: string; kind: 'requirement' | 'file' }[] = []
  if (requirementDoc.value) {
    links.push({ name: requirementDoc.value.brand?.name || '需求文档', icon: '📄', kind: 'requirement' })
  }
  for (const a of projectArtifacts.value) {
    if (!a.files) continue
    for (const name of Object.keys(a.files)) {
      links.push({ name, icon: iconForFile(name), kind: 'file' })
    }
  }
  return links
})

// 点击文字产物链接: 展开右侧面板并联动选中对应文件 + 打开预览
function openArtifact(name: string, kind: 'requirement' | 'file') {
  if (rightCollapsed.value) rightCollapsed.value = false
  const target = kind === 'requirement' ? '__requirement_doc__' : name
  nextTick(() => rightPanel.value?.selectFile(target))
}

const pendingSend = ref(false)
const pendingRetry = ref<{ suggested: string[]; message: string } | null>(null)
const lastSentText = ref('')

// ---- 多意图编排(P3): 泳道进度 + 合并结果 ----
const isMultiIntent = ref(false)
const orchStrategy = ref<'parallel' | 'mixed'>('parallel')
const subTasks = ref<SubTaskView[]>([])
const mergeResult = ref<{ success_count: number; fail_count: number; failed_tasks: FailedSubTask[] } | null>(null)
// 已确认放行的子任务 id(中风险确认后重发时带上, 跨重发保留)
const confirmedSubtaskIds = ref<string[]>([])

const auth = useAuth()
const projectStore = useProjectStore()
const convStore = useConversationStore()

// 当有新消息时自动追底(除非用户手动上滚查看历史)
watch(() => convStore.messages.length, () => {
  if (autoScroll) scrollToBottom(true)
})
// 生成中 token 持续追加也追底
watch(generatedHtml, () => {
  if (autoScroll && generating.value) scrollToBottom(false)
})

// 所有会话统一消息流(微信风格: 最老的在上方, 最新的在最下方)
// 数组顺序: [oldest_session, ..., current_session], 配合 flex-direction: column 渲染
const allSessions = computed(() => {
  const past = convStore.pastSessions.map(s => ({
    conv: s.conv, loading: s.loading,
    msgs: s.messages.length ? s.messages : [],
  }))
  const cur = convStore.conversations[0]
  if (cur && convStore.currentConvId === cur.id && !past.some(p => p.conv.id === cur.id)) {
    past.unshift({ conv: cur, loading: false, msgs: convStore.messages })
  }
  // 逆转: pastSessions 是降序(新→旧)追加的, 需要反转为升序(旧→新)以符合微信风格
  return past.reverse()
})

const currentProjectName = computed(
  () =>
    projectStore.projects.find((p) => p.id === projectStore.currentProjectId)?.name || '未选择项目',
)
const currentProjectDate = computed(
  () =>
    projectStore.projects.find((p) => p.id === projectStore.currentProjectId)?.created_at?.slice(0, 10) || '',
)

// 项目名行内编辑
const editingProject = ref(false)
const editProjectName = ref('')
const projInput = ref<HTMLInputElement | null>(null)
function startEditProject() {
  editProjectName.value = currentProjectName.value
  editingProject.value = true
  nextTick(() => projInput.value?.focus())
}
async function saveProjectName() {
  if (!editingProject.value) return
  const name = editProjectName.value.trim()
  if (name && name !== currentProjectName.value && projectStore.currentProjectId) {
    await renameProject(projectStore.currentProjectId, name)
    await projectStore.load()
  }
  editingProject.value = false
}

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

// ---- 生成中消息本地快照(刷新/崩溃恢复) ----
const DRAFT_KEY_PREFIX = 'seedai:draft:'

function saveDraft(convId: number) {
  try {
    const msgs = convStore.messages.filter(m => m.content)
    if (msgs.length) {
      sessionStorage.setItem(DRAFT_KEY_PREFIX + convId, JSON.stringify(msgs))
    }
  } catch { /* 忽略 */ }
}

function clearDraft() {
  if (convStore.currentConvId) {
    sessionStorage.removeItem(DRAFT_KEY_PREFIX + convStore.currentConvId)
  }
}

function loadDraft(): boolean {
  if (!convStore.currentConvId) return false
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY_PREFIX + convStore.currentConvId)
    if (!raw) return false
    const msgs = JSON.parse(raw) as Message[]
    if (msgs.length > convStore.messages.length) {
      convStore.messages = msgs
      return true
    }
  } catch { /* 忽略 */ }
  return false
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
  currentIntent.value = { level1: '', level2: '' }
  pendingRetry.value = null
  awaitingConfirm.value = false
  awaitPlan.value = null
  // 多意图编排状态(新请求时重置;confirmedSubtaskIds 在 doSend 单独清)
  isMultiIntent.value = false
  subTasks.value = []
  mergeResult.value = null
  // 非阻塞候选提示(新请求时清除)
  alternativesData.value = null
}

function upsertStep(stage: string, status: ThoughtStep['status'], customLabel?: string) {
  const label = customLabel || STAGE_LABELS[stage] || stage
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
    onToken: (t, subTaskId) => {
      // 多意图: 合并结果(__merge__)进入主气泡; 子任务自身 token 进入对应泳道流式预览
      if (subTaskId === '__merge__') {
        const m = convStore.messages[assistantIdx]
        if (m) {
          m.content += t
          if (m.content.length % 40 === 0) saveDraft(convStore.currentConvId!)
        }
        return
      }
      if (subTaskId) {
        const st = subTasks.value.find((s) => s.id === subTaskId)
        if (st) {
          st.tokens += t
          return
        }
      }
      // 单意图遗留路径(无 sub_task_id): 同原行为
      // 仅 builder_agent 的 token 才是生成的 HTML 代码
      if (currentIntent.value.level1 === 'build') generatedHtml.value += t
      const m = convStore.messages[assistantIdx]
      if (m) {
        m.content += t
        // 每 10 个 token 存一次本地快照(减开销)
        if (m.content.length % 40 === 0) saveDraft(convStore.currentConvId!)
      }
    },
    onPreview: (d) => {
      if (d.url) previewUrl.value = d.url as string
    },
    onDegraded: () => {
      degraded.value = true
    },
    onRequirement: (d) => {
      console.log('[SSE] 收到需求文档:', (d.data as any)?.brand?.name)
      requirementDoc.value = (d.data as Record<string, any>) || null
      // 主动在预览页展示需求文档(若面板折叠则先展开)
      if (rightCollapsed.value) rightCollapsed.value = false
      nextTick(() => rightPanel.value?.selectFile('__requirement_doc__'))
    },
    onDone: () => {
      generating.value = false
      finished.value = true
      thoughtSteps.value = []
      planNodes.value = []
      awaitingConfirm.value = false
      awaitPlan.value = null
      clearActiveGen()
      clearDraft()
      loadArtifacts()
      // 从 DB 同步当前会话消息(替换乐观更新的 id:0)
      if (projectStore.currentProjectId) {
        convStore.loadConversations(projectStore.currentProjectId).then(() => scrollToBottom(false))
      }
      dequeueAndSend()
    },
    onAborted: () => {
      generating.value = false
      finished.value = true
      thoughtSteps.value = []
      planNodes.value = []
      awaitingConfirm.value = false
      awaitPlan.value = null
      errorMsg.value = '已取消'
      clearActiveGen()
      if (projectStore.currentProjectId) {
        convStore.loadConversations(projectStore.currentProjectId).then(() => scrollToBottom(false))
      }
      dequeueAndSend()
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
    onIntent: (d) => {
      currentIntent.value = { level1: d.level1 || '', level2: d.level2 || '' }
      // 在思考时间线顶部插入意图识别步骤(两级显示)
      const lbl = d.level2_label ? `${d.level1_label || ''} → ${d.level2_label}` : (d.label || '')
      if (lbl) upsertStep('intent_recognized', 'done', lbl)
    },
    onOptions: (d: OptionEvent) => {
      optionsData.value = d
      selectedOption.value = ''
      showOptionsModal.value = true
    },
    onAlternatives: (d: AlternativesEvent) => {
      // 非阻塞: 系统已自决 top-1 并继续生成; 仅记录候选供用户随时切换
      alternativesData.value = d
      console.log('[SSE] alternatives 已选=%s 可切换=%s', d.selected, d.skills)
    },
    onUnsupported: () => {
      generating.value = false
      finished.value = true
      errorMsg.value = '暂不支持此功能，请尝试其他类型请求'
      clearActiveGen()
    },
    onBlock: (d: BlockEvent) => {
      generating.value = false
      finished.value = true
      blockReason.value = d.reason || '该操作存在高风险，已被安全策略拦截'
      clearActiveGen()
    },
    onConfirm: (d: ConfirmEvent) => {
      generating.value = false
      finished.value = true
      pendingConfirm.value = { reason: d.reason || '该操作需二次确认', skill: d.skill || '' }
      showConfirmModal.value = true
      clearActiveGen()
    },
    onPaused: (d: any) => {
      if (d.stage === 'await_confirm') {
        generating.value = false
        finished.value = true
        // 不再弹模态框: 标记为等待文字回复, 由产物链接卡片引导用户回复/改进
        awaitingConfirm.value = true
        awaitPlan.value = {
          title: d.plan_title || '',
          goal: d.plan_goal || '',
          steps: d.plan_steps || [],
        }
        clearActiveGen()
      }
    },
    onError: (m) => {
      generating.value = false
      finished.value = true
      thoughtSteps.value = []
      planNodes.value = []
      awaitingConfirm.value = false
      awaitPlan.value = null
      errorMsg.value = m
      clearActiveGen()
    },
    onQc: (data: QcResult) => {
      // 后置 QC 三裁判结果:按当前 trace_id 存入 qcMap, 气泡徽标读取
      if (traceId.value) qcMap[traceId.value] = data
    },
    // ---- 多意图编排事件(P3) ----
    onOrchestration: (d: OrchestrationEvent) => {
      isMultiIntent.value = true
      orchStrategy.value = d.strategy
      subTasks.value = d.tasks.map((t) => ({ ...t, tokens: '', artifacts: [] }))
      mergeResult.value = null
    },
    onSubtaskStart: (d: SubTaskStartEvent) => {
      const st = subTasks.value.find((s) => s.id === d.sub_task_id)
      if (st) {
        st.status = 'running'
        st.layer = d.layer
        st.goal = d.goal
        st.skill = d.skill
        st.risk = d.risk
      }
    },
    onSubtaskDone: (d: SubTaskDoneEvent) => {
      const st = subTasks.value.find((s) => s.id === d.sub_task_id)
      if (st) {
        st.status = 'done'
        st.result_summary = d.result_summary
        st.artifacts = d.artifacts || []
      }
    },
    onSubtaskFail: (d: SubTaskFailEvent) => {
      const st = subTasks.value.find((s) => s.id === d.sub_task_id)
      if (st) {
        // 状态按原因细分(兜底 5: 风险分级)
        if (d.reason.includes('高风险')) st.status = 'blocked'
        else if (d.reason.includes('中风险')) st.status = 'skipped'
        else st.status = 'failed'
        st.fail_reason = d.reason
        st.recoverable = d.recoverable
      }
    },
    onMerge: (d: MergeEvent) => {
      // 合并正文已在 __merge__ token 流中累积到主气泡; 此处仅记录部分失败清单
      mergeResult.value = {
        success_count: d.success_count,
        fail_count: d.fail_count,
        failed_tasks: d.failed_tasks,
      }
    },
  }
}

async function loadCurrentProject() {
  const pid = projectStore.currentProjectId
  if (pid == null) return
  await convStore.loadConversations(pid)
}

async function send() {
  let text = input.value.trim()
  // 如果有待发送的选项, 拼接到消息前面
  if (pendingOptionsText.value) {
    text = pendingOptionsText.value + '\n' + text
    pendingOptionsText.value = ''
  }
  if (!text) return
  // 非阻塞候选提示激活时, 若输入是选择 token(如 "B"/"2"/"选B") → 直接切换 skill
  // 对齐后端 pending_options(完整候选列表: [已选top1, ...alts]); "A"=重新确认当前(忽略), "B"=alts[0]
  if (alternativesData.value && alternativesData.value.skills.length) {
    const full = [alternativesData.value.selected, ...alternativesData.value.skills]
    const idx = parseSelectionToken(text)
    if (idx !== null && idx > 0 && idx < full.length) {
      const skill = full[idx]
      input.value = ''
      alternativesData.value = null
      console.log('[发送] 命中候选切换 token=%s → skill=%s', text, skill)
      resendWithSkill(skill)
      return
    }
  }
  // 生成中: 加入队列
  if (generating.value) { enqueue(text); return }
  // 等待方案确认: 直接回复文字 = 续跑生成(可带改进意见, 也可直接"开始/确认")
  if (awaitingConfirm.value) {
    input.value = ''
    await resumeFromPlan(text)
    return
  }
  // 鉴权
  if (!auth.user.value) { pendingSend.value = true; auth.openLogin(); return }
  input.value = ''
  await doSend(text)
}

async function doSend(text: string) {
  const pid = projectStore.currentProjectId
  if (pid == null) { alert('请先在左侧新建项目'); return }

  // 防重复: 只有 id 不存在或消息为空时才创建新会话
  if (convStore.currentConvId == null || convStore.messages.length === 0) {
    // 防止并发创建——检查 conversations 是否已有 pending 的 API 调用
    if (!convStore.creating) {
      await convStore.create(pid, text.slice(0, 20))
    }
  }

  resetGenState()
  traceId.value = genTraceId()
  const cid = convStore.currentConvId!
  setActiveGen(cid, traceId.value)

  // ---- WebLLM 上下文检测 ----
  let contextHint: string | undefined
  try {
    const { contextCheck } = await import('../webllm/context')
    contextHint = await contextCheck(text, convStore.messages.slice(-20).map(m => ({ role: m.role, content: m.content }))) || undefined
  } catch { /* 降级 */ }

  // ---- WebLLM 本地分类 ----
  let intent: { level1: string; level2: string } | null = null
  try {
    const { localClassify } = await import('../webllm/classifier')
    intent = await localClassify(text)
  } catch { /* 降级: 走服务端 */ }

  // ---- 本地闲聊(casual/explain) ----
  if (intent && intent.level1 === 'learn' && intent.level2 === 'casual') {
    try {
      const { localChat } = await import('../webllm/chat')
      const chatMsgs = convStore.messages.slice(-6).map(m => ({ role: m.role, content: m.content }))
      const reply = await localChat([...chatMsgs, { role: 'user', content: text }])
      if (reply) {
        console.log(`[WebLLM] 本地闲聊完成 → 不走服务端`)
        convStore.messages.push({ role: 'user', content: text, conversation_id: cid, id: 0, created_at: '' } as any)
        convStore.messages.push({ role: 'assistant', content: reply, conversation_id: cid, id: 0, created_at: '' } as any)
        nextTick(scrollToBottom)
        return
      }
    } catch { /* 降级: 走服务端 */ }
  }
  if (intent) {
    console.log(`[WebLLM] 分类结果: ${intent.level1}/${intent.level2} → 路由服务端`)
  } else {
    console.log('[WebLLM] 本地分类不可用 → 走服务端')
  }

  // 多意图: 全新用户请求, 清空之前累计的已确认子任务
  confirmedSubtaskIds.value = []

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
    traceId: traceId.value,
    conversationId: cid,
    q: text,
    contextHint,
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
    await convStore.loadConversations(projectStore.currentProjectId!)
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

// 气泡内多维度评价提交(v0.8.5 M1): 按 trace_id 存储, 落库 + 前端即时反馈
async function onRate(tid: string, p: { rating: number; comment: string; dimensions: RatingDims }) {
  if (!tid) return
  const ok = await sendFeedback(
    tid,
    p.rating,
    convStore.currentConvId ?? undefined,
    p.comment || undefined,
    p.dimensions,
  )
  if (ok) {
    ratedMap[tid] = { rating: p.rating, dims: p.dimensions || {}, comment: p.comment || '' }
  }
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
  await auth.init()
  const m = await fetchModels()
  if (m.length) models.value = m
  // WebLLM 后台预热(不阻塞页面)
  import('../webllm/engine').then(({ initEngine }) => {
    initEngine((pct) => {
      if (pct === 100) console.log('[WebLLM] 就绪')
      else if (pct % 10 === 0) console.log(`[WebLLM] 下载 ${pct}%`)
    })
  }).catch(() => {})
  if (auth.user.value) {
    await projectStore.load()
    await loadCurrentProject()
    await loadArtifacts()
    await nextTick(() => { setupScrollLoading(); scrollToBottom(false) })
    // 恢复未完成的本地草稿(刷新/崩溃后)
    if (loadDraft()) scrollToBottom(false)
    await maybeResume()
  }
})
onUnmounted(() => { teardownScrollLoading() })

watch(
  () => projectStore.currentProjectId,
  async (id) => {
    if (id != null) {
      await convStore.loadConversations(id)
      await loadArtifacts()
      await nextTick(() => { setupScrollLoading(); scrollToBottom(false) })
      await maybeResume()
    }
  },
)

watch(
  () => convStore.pendingConvId,
  async (id) => {
    if (id != null) {
      await convStore.loadConversations(projectStore.currentProjectId!)
      convStore.pendingConvId = null
      autoScroll = true
      scrollToBottom(false)
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
    <div class="left-col" :class="{ full: rightCollapsed }">
      <div class="conv-bar">
        <span class="proj">📁</span>
        <span v-if="!editingProject" class="proj-name">{{ currentProjectName }}</span>
        <input
          v-else
          ref="projInput"
          v-model="editProjectName"
          class="proj-input"
          @keyup.enter="saveProjectName"
          @blur="saveProjectName"
          @keyup.escape="editingProject = false"
        />
        <button class="proj-edit" title="修改项目名" data-track="重命名项目" @click="startEditProject">✏️</button>
        <span class="proj-date">{{ currentProjectDate }}</span>
      </div>

      <!-- 断点续跑横幅(§7) -->
      <div v-if="pausedConv" class="paused-banner">
        <span>⚠ 未完成的生成 · {{ pausedConv.checkpoint_stage || '?' }} · 已完成 {{ pausedConv.progress_pct || 0 }}%</span>
        <button class="paused-resume" @click="resumeConversation">继续生成</button>
        <button class="paused-abort" @click="abortPaused">放弃</button>
      </div>

      <div ref="convRef" class="conv" @scroll="onConvScroll">
        <div v-if="convStore.loadingMore" class="loading-more">加载更早的会话…</div>
        <div ref="sentinel" class="sentinel"></div>

        <!-- 全新项目无任何会话 -->
        <div v-if="convStore.conversations.length === 0" class="empty">
          在下方输入你想做的事，AI 会智能识别意图并给出回复。
        </div>

        <!-- 所有会话统一消息流(当前+历史同一格式, 会话间仅 thin 分割线) -->
        <template v-for="(s, si) in allSessions" :key="s.conv.id">
          <div v-if="si > 0" class="session-divider">
            <span>{{ s.conv.title || '会话' }} · {{ s.conv.updated_at?.slice(0, 10) || '' }}</span>
          </div>
          <div v-if="s.loading" class="loading-more">加载中…</div>
          <MessageBubble
            v-for="(m, i) in s.msgs"
            :key="`s${si}-${i}`"
            :role="m.role"
            :content="m.content"
            :time="m.role === 'user' ? (m.created_at || '') : ''"
            :trace-id="m.trace_id || undefined"
            :qc="m.trace_id ? (qcMap[m.trace_id] ?? null) : null"
            :my-rating="m.trace_id && ratedMap[m.trace_id] ? ratedMap[m.trace_id].rating : null"
            :my-dims="m.trace_id && ratedMap[m.trace_id] ? ratedMap[m.trace_id].dims : null"
            :my-comment="m.trace_id && ratedMap[m.trace_id] ? ratedMap[m.trace_id].comment : null"
            :can-rate="!!auth.user"
            @rate="(p) => m.trace_id && onRate(m.trace_id, p)"
          />
        </template>
      </div>

      <div
        v-if="(generating && !isMultiIntent && (thoughtSteps.length || planNodes.length)) || (isMultiIntent && subTasks.length)"
        class="trail-wrap"
      >
        <ThoughtTrail
          v-if="!isMultiIntent"
          :steps="thoughtSteps"
          :plans="planNodes"
          :degraded="degraded"
          :current="currentStage"
          :intent="currentIntent"
        />
        <template v-else>
          <SubTaskTrack
            :subtasks="subTasks"
            :strategy="orchStrategy"
            @confirm="confirmSubTask"
          />
          <MergedResult
            v-if="mergeResult"
            :success-count="mergeResult.success_count"
            :fail-count="mergeResult.fail_count"
            :failed-tasks="mergeResult.failed_tasks"
          />
        </template>
      </div>

      <div class="footer">
        <!-- 消息队列(生成中等待发送) -->
        <div v-if="queueVisible" class="queue-bar">
          <div class="queue-head">⏳ 等待发送 ({{ msgQueue.length }})</div>
          <div v-for="(q, i) in msgQueue" :key="i" class="queue-row">
            <span class="queue-seq">#{{ i + 1 }}</span>
            <template v-if="q.editing">
              <input
                :ref="(el: any) => el && q.editing && (el as HTMLInputElement).focus()"
                class="queue-input"
                :value="q.text"
                @keyup.enter="saveQueueItem(i, ($event.target as HTMLInputElement).value)"
                @blur="saveQueueItem(i, ($event.target as HTMLInputElement).value)"
                @keyup.escape="q.editing = false"
              />
            </template>
            <template v-else>
              <span class="queue-text">{{ q.text }}</span>
            </template>
            <span class="queue-actions">
              <button class="qbtn" title="立即发送" @click="sendNowQueueItem(i)">▶</button>
              <button class="qbtn" title="编辑" @click="editQueueItem(i)">✏️</button>
              <button class="qbtn qdel" title="删除" @click="deleteQueueItem(i)">✕</button>
            </span>
          </div>
        </div>
        <div v-if="pendingOptionsText" class="pending-opt-badge">
          📌 待发送: {{ pendingOptionsText }}
          <button class="pob-clear" @click="pendingOptionsText = ''">✕ 清除</button>
        </div>
        <ChatInput
          v-model:value="input"
          v-model:model="model"
          :generating="generating"
          :models="models"
          @send="send"
          @stop="stop"
        />
        <div v-if="errorMsg" class="error">⚠ {{ errorMsg }}</div>
        <!-- 非阻塞候选提示: 系统已自决 top1 并继续, 列出可切换候选(可点击或输入"用 X"/"B") -->
        <div v-if="alternativesData && alternativesData.skills.length" class="alts-bar">
          <span class="alts-label">已选 <b>{{ alternativesData.selected }}</b></span>
          <span class="alts-sep">·</span>
          <span class="alts-hint">可切换：</span>
          <button
            v-for="(sk, i) in alternativesData.skills"
            :key="sk"
            class="alts-chip"
            @click="resendWithSkill(sk)"
          >{{ String.fromCharCode(66 + i) }}. {{ sk }}</button>
          <span class="alts-tip">（输入字母如「B」或「用 {{ alternativesData.skills[0] }}」也可切换）</span>
        </div>
        <!-- 方案已就绪: 文字产物链接(替代确认模态框) -->
        <div v-if="awaitingConfirm" class="artifact-links-card">
          <div class="alc-head">📦 需求方案已生成，预览已自动打开</div>
          <div class="alc-hint">
            💡 直接回复文字即可：说明改进方向（如「加个博客列表页」），或回复「开始 / 确认」立即生成代码。
          </div>
          <div class="alc-list">
            <button
              v-for="link in artifactLinks"
              :key="link.name + link.kind"
              class="alc-item"
              @click="openArtifact(link.name, link.kind)"
            >
              <span class="alc-icon">{{ link.icon }}</span>
              <span class="alc-name">{{ link.name }}</span>
              <span class="alc-open">打开 ↗</span>
            </button>
          </div>
          <div v-if="awaitPlan && awaitPlan.steps.length" class="alc-plan">
            <div class="alc-plan-title">📋 {{ awaitPlan.title || '方案概览' }}</div>
            <div class="alc-goal">{{ awaitPlan.goal }}</div>
            <ol class="alc-steps">
              <li v-for="(s, i) in awaitPlan.steps" :key="i">{{ s }}</li>
            </ol>
          </div>
        </div>
        <!-- 方案选择弹窗(单选, 确认后记录, 下次 send 一起发送) -->
        <div v-if="showOptionsModal && optionsData" class="options-modal-backdrop" @click.self="cancelOptions" @keydown.escape="cancelOptions">
          <div class="options-modal">
            <div class="om-title">{{ optionsData.question || '请选择方案' }}</div>
            <div class="om-choices">
              <label
                v-for="c in optionsData.choices"
                :key="c.id"
                class="om-choice"
                :class="{ on: selectedOption === c.id }"
              >
                <input
                  v-model="selectedOption"
                  type="radio"
                  :value="c.id"
                  class="om-radio"
                />
                <div class="om-info">
                  <div class="om-name">{{ c.id }}. {{ c.title }}</div>
                  <div v-if="c.desc" class="om-desc">{{ c.desc }}</div>
                  <div v-if="c.pros" class="om-pros">✅ {{ c.pros }}</div>
                  <div v-if="c.cons" class="om-cons">⚠️ {{ c.cons }}</div>
                </div>
              </label>
            </div>
            <div class="om-actions">
              <button class="om-btn om-confirm" :disabled="!selectedOption" @click="onOptionsConfirm">确认选择</button>
              <button class="om-btn om-cancel" @click="cancelOptions">取消</button>
            </div>
          </div>
        </div>
        <!-- 高危拦截提示(安全 critical, 不可绕过) -->
        <div v-if="blockReason" class="error block-warn">
          🛑 高危操作已被拦截：{{ blockReason }}
          <button class="pob-clear" @click="blockReason = ''">✕ 我知道了</button>
        </div>
        <!-- 二次确认对话框(安全 high) -->
        <div v-if="showConfirmModal && pendingConfirm" class="confirm-plan">
          <div class="cp-title">⚠️ 安全确认</div>
          <div class="cp-body">
            <div class="cp-goal">{{ pendingConfirm.reason }}</div>
          </div>
          <div class="cp-actions">
            <button class="cp-btn cp-confirm" @click="resendConfirmed">✅ 确认继续</button>
            <button class="cp-btn cp-cancel" @click="showConfirmModal = false; pendingConfirm = null">取消</button>
          </div>
        </div>
        <div v-if="finished && !errorMsg && !blockReason" class="feedback">
          <span class="rate-hint-text">评分已移到每条 AI 回复气泡内，点击「⭐ 评价」即可多维度打分</span>
          <button
            v-if="previewUrl"
            class="copy-link"
            title="复制预览链接"
            data-track="复制预览链接"
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

    <div class="right-pane" :class="{ collapsed: rightCollapsed }">
      <div class="right-toggle">
        <button class="toggle-btn" @click="rightCollapsed = !rightCollapsed">
          {{ rightCollapsed ? '◀' : '▶' }}
        </button>
        <span v-if="rightCollapsed" class="toggle-label">预览</span>
      </div>
      <div v-if="!rightCollapsed" class="right-body">
      <RightPanel
        ref="rightPanel"
        :artifacts="projectArtifacts"
        :generating="generating"
        :previewUrl="previewUrl"
        :projectId="projectStore.currentProjectId"
        :requirementDoc="requirementDoc"
        @refresh="loadArtifacts"
      />
      </div>
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
.left-col.full {
  max-width: 100%;
}
.right-pane {
  width: 46%;
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  transition: width 0.2s;
}
.right-pane.collapsed {
  width: 36px;
}
.right-toggle {
  display: flex;
  align-items: center;
  padding: 6px 8px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.toggle-btn {
  background: none; border: none; cursor: pointer;
  font-size: 18px; color: var(--text-muted); font-weight: bold;
  padding: 8px 12px; line-height: 1;
}
.toggle-label {
  writing-mode: vertical-rl;
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 8px;
}
.right-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
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
.conv-title {
  font-size: 13px;
  color: var(--muted);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.proj-name { font-weight: 700; font-size: 14px; color: #1e293b; }
.proj-input { font-weight: 700; font-size: 14px; border: 1px solid var(--brand); border-radius: 6px; padding: 2px 6px; color: #1e293b; background: #fff; outline: none; width: 180px; }
.proj-edit { border: none; background: none; cursor: pointer; font-size: 13px; padding: 2px 4px; opacity: .5; transition: opacity .15s; }
.proj-edit:hover { opacity: 1; }
.proj-date { font-size: 11px; color: var(--muted); margin-left: auto; }
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
.sentinel { height: 1px; }
.loading-more { text-align: center; color: var(--muted); font-size: 12px; padding: 8px; }

/* 历史会话折叠卡片 */
.session-divider { text-align: center; padding: 12px 0 6px; margin: 0 8px 6px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); }
.session-divider span { background: #f8fafc; padding: 2px 10px; border-radius: 4px; }
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
.confirm-plan {
  background: #f0f9ff;
  border: 1px solid #bae6fd;
  border-radius: 10px;
  padding: 14px;
  margin: 8px 0;
}
.cp-title { font-weight: 600; font-size: 14px; margin-bottom: 10px; color: #0369a1; }
.cp-body { margin-bottom: 12px; }
.cp-goal { font-size: 13px; color: #475569; margin-bottom: 8px; }
.cp-steps { margin: 0; padding-left: 18px; }
.cp-steps li { font-size: 12px; color: #64748b; line-height: 1.8; }
.cp-actions { display: flex; gap: 8px; }
.cp-btn { border: none; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 13px; font-weight: 600; }
.cp-confirm { background: #0284c7; color: #fff; }
.cp-confirm:hover { background: #0369a1; }
.cp-cancel { background: #f1f5f9; color: #64748b; }
.cp-cancel:hover { background: #e2e8f0; }

/* ── 文字产物链接卡片(替代确认模态框) ── */
.artifact-links-card {
  background: #f0f9ff;
  border: 1px solid #bae6fd;
  border-radius: 10px;
  padding: 12px 14px;
  margin: 8px 0;
}
.alc-head { font-weight: 600; font-size: 14px; color: #0369a1; margin-bottom: 6px; }
.alc-hint {
  font-size: 12px; color: #475569; line-height: 1.6;
  background: #fff; border: 1px dashed #bae6fd; border-radius: 8px;
  padding: 6px 10px; margin-bottom: 10px;
}
.alc-list { display: flex; flex-wrap: wrap; gap: 8px; }
.alc-item {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid #bae6fd; background: #fff; border-radius: 8px;
  padding: 5px 10px; cursor: pointer; font-size: 12px; color: #0f172a;
  transition: background .15s, transform .12s, box-shadow .15s;
}
.alc-item:hover {
  background: #e0f2fe; transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(2,132,199,.18);
}
.alc-icon { font-size: 14px; }
.alc-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
.alc-open { font-size: 11px; color: #0284c7; font-weight: 600; }
.alc-plan {
  margin-top: 10px; background: #fff; border: 1px solid #e0f2fe;
  border-radius: 8px; padding: 8px 12px;
}
.alc-plan-title { font-size: 13px; font-weight: 600; color: #0f172a; margin-bottom: 4px; }
.alc-goal { font-size: 12px; color: #475569; margin-bottom: 6px; }
.alc-steps { margin: 0; padding-left: 18px; }
.alc-steps li { font-size: 12px; color: #64748b; line-height: 1.7; }

/* ── 方案选择弹窗 ── */
.options-modal-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.options-modal {
  background: var(--bg); border-radius: 12px;
  padding: 24px; max-width: 440px; width: 90vw;
  box-shadow: 0 8px 32px rgba(0,0,0,.18);
}
.om-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; color: var(--text); }
.om-choices { display: flex; flex-direction: column; gap: 10px; }
.om-choice {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 12px; border-radius: 8px; border: 1px solid var(--border);
  cursor: pointer; transition: border-color .2s, background .2s;
}
.om-choice.on { border-color: #3b82f6; background: rgba(59,130,246,.08); }
.om-choice:hover { border-color: #93c5fd; background: rgba(59,130,246,.04); }
.om-radio { margin-top: 3px; accent-color: #3b82f6; }
.om-info { flex: 1; min-width: 0; }
.om-name { font-weight: 600; font-size: 14px; color: var(--text); }
.om-desc { font-size: 12px; color: var(--muted); margin-top: 2px; }
.om-pros { font-size: 12px; color: #16a34a; margin-top: 4px; }
.om-cons { font-size: 12px; color: #dc2626; margin-top: 2px; }
.om-actions { display: flex; gap: 8px; margin-top: 18px; justify-content: flex-end; }
.om-btn { border: none; border-radius: 6px; padding: 8px 18px; cursor: pointer; font-size: 13px; font-weight: 600; }
.om-confirm { background: #3b82f6; color: #fff; }
.om-confirm:disabled { opacity: .5; cursor: not-allowed; }
.om-confirm:not(:disabled):hover { background: #2563eb; }
.om-cancel { background: #f1f5f9; color: #64748b; }
.om-cancel:hover { background: #e2e8f0; }

/* ── 非阻塞候选提示条 ── */
.alts-bar {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  padding: 8px 14px; margin: 0 16px 6px;
  background: rgba(16,185,129,.08); border: 1px solid #6ee7b7;
  border-radius: 10px; font-size: 13px; color: #047857;
}
.alts-label b { color: #065f46; }
.alts-sep { color: #94a3b8; }
.alts-hint { color: #059669; }
.alts-chip {
  border: 1px solid #6ee7b7; background: #fff; color: #047857;
  border-radius: 999px; padding: 4px 12px; cursor: pointer;
  font-size: 12px; font-weight: 600; transition: all .18s ease;
}
.alts-chip:hover { background: #10b981; color: #fff; transform: translateY(-1px); }
.alts-tip { color: #94a3b8; font-size: 11px; }

/* ── 待发送选项提示 ── */
.pending-opt-badge {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; margin: 0 16px 4px;
  background: rgba(59,130,246,.1); border: 1px solid #93c5fd;
  border-radius: 6px; font-size: 12px; color: #1d4ed8;
}
.pob-clear {
  margin-left: auto; border: none; background: none;
  color: #94a3b8; cursor: pointer; font-size: 12px;
}
.pob-clear:hover { color: #dc2626; }

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
.paused-banner { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: #fef3c7; border: 1px solid #f59e0b; border-radius: 10px; font-size: 13px; margin-bottom: 8px; }
.paused-resume { padding: 4px 12px; border: none; border-radius: 6px; background: #10b981; color: #fff; cursor: pointer; font-size: 12px; }
.paused-abort { padding: 4px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); cursor: pointer; font-size: 12px; }
.queue-bar { margin-bottom: 8px; padding: 8px 10px; background: #eef2ff; border-radius: 8px; font-size: 12px; max-height: 200px; overflow-y: auto; }
.queue-head { font-weight: 700; color: #4f46e5; margin-bottom: 4px; }
.queue-row { display: flex; align-items: center; gap: 6px; padding: 3px 0; border-bottom: 1px solid #e0e7ff; }
.queue-row:last-child { border-bottom: none; }
.queue-seq { color: #818cf8; font-weight: 600; min-width: 20px; }
.queue-text { flex: 1; color: #334155; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.queue-input { flex: 1; border: 1px solid #a5b4fc; border-radius: 4px; padding: 1px 4px; font-size: 12px; outline: none; }
.queue-actions { display: flex; gap: 2px; }
.qbtn { border: none; background: none; cursor: pointer; font-size: 13px; padding: 1px 4px; border-radius: 3px; color: var(--muted); }
.qbtn:hover { background: #ddd6fe; color: #4f46e5; }
.qdel:hover { background: #fee2e2; color: #ef4444; }
</style>
