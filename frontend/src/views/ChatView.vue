<script setup lang="ts">
// ChatView —— 对话主页面(核心组件)。
// 职责:
//   1. 组装「项目 → 会话 → 消息」三级数据(经 project / conversation 两个 Pinia store);
//   2. 发送:校验登录态与项目/会话,调用 startChat 建立 SSE 流,把流事件映射为本地状态;
//   3. 鉴权门禁:未登录时点发送 -> 记 pendingSend 并弹登录框;登录成功后自动重发(见底部 watch);
//   4. 取消:stop() 级联 cancelChat -> 业务 -> AI 中断生成(C1);
//   5. 评价:生成完成后可对本次 trace 投 👍/👎(数据供后端统计 + 回归集)。
// 左栏是对话区 + 思考轨迹,右栏是实时预览(PreviewPane)。
import { computed, onMounted, ref, watch } from 'vue'
import ThoughtTrail from '../components/ThoughtTrail.vue'
import PreviewPane from '../components/PreviewPane.vue'
import ChatInput from '../components/ChatInput.vue'
import MessageBubble from '../components/MessageBubble.vue'
import { useRouter } from 'vue-router'
import { startChat, cancelChat, fetchModels, sendFeedback, type ChatCallbacks } from '../api/chat'
import { useAuth } from '../composables/useAuth'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import type { ModelInfo } from '../types'

// ---- 本地 UI 状态 ----
// generating:是否正在生成(控制发送/停止按钮切换、预览 loading);
// finished:本次生成是否已结束(用于显示评价条);
// stages/thinks:思考轨迹(ThoughtTrail 用);generatedHtml:流式累积的 HTML;
// previewUrl:上游给出的线上预览直链(COS);traceId:本次链路 id,用于取消与评价;
// pendingSend:登录门禁的"待重发"标记 —— 未登录时点发送会置 true,登录成功 watch 触发重发。
const models = ref<ModelInfo[]>([])
const model = ref('hy3')
const input = ref('')
const generating = ref(false)
const finished = ref(false)

const stages = ref<string[]>([])
const currentStage = ref('')
const thinks = ref('')
const degraded = ref(false)
const generatedHtml = ref('')
const previewUrl = ref<string | null>(null)
const errorMsg = ref('')
const traceId = ref('')
const esRef = ref<EventSource | null>(null)
const rating = ref<'' | 'up' | 'down'>('')

const pendingSend = ref(false)

const auth = useAuth()
const projectStore = useProjectStore()
const convStore = useConversationStore()
const router = useRouter()

const messages = computed(() => convStore.messages)
const currentProjectName = computed(
  () =>
    projectStore.projects.find((p) => p.id === projectStore.currentProjectId)?.name || '未选择项目',
)

// 生成本次对话的链路 id(trace_id):
// 优先用 crypto.randomUUID(安全随机);老浏览器无该 API 时退化为"时间戳+随机"拼接,
// 仅用于取消/评价时与后端对齐同一路生成,不要求密码学强度。
function genTraceId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 't' + Date.now().toString(16) + Math.random().toString(16).slice(2)
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
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || generating.value) return
  // 鉴权门禁:未登录不发送,记 pendingSend 并弹登录框。
  // 登录成功后由底部 watch(auth.user) 检测到 user 变化,清 pendingSend 并重调 send()。
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

  // 重置本次生成状态(避免与上一轮残留混淆)
  stages.value = []
  currentStage.value = ''
  thinks.value = ''
  degraded.value = false
  generatedHtml.value = ''
  previewUrl.value = null
  errorMsg.value = ''
  finished.value = false
  rating.value = ''
  traceId.value = genTraceId()

  // 乐观更新:先把用户消息 + 一个空 assistant 占位塞进消息列表,
  // 后续 token 事件直接累加到 assistant 占位上,实现"打字机"效果。
  const cid = convStore.currentConvId!
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
  input.value = ''

  // 把 SSE 事件映射到本地状态:node=阶段进度 / think=思考累积 / token=HTML 累积 /
  // preview=线上预览直链 / degraded=模型降级 / done=结束 / aborted=取消 / error=报错。
  const cb: ChatCallbacks = {
    onNode: (d) => {
      if (d.stage) {
        currentStage.value = d.stage
        if (d.stage && !stages.value.includes(d.stage)) stages.value.push(d.stage)
        if (d.stage === 'preview' && d.url) previewUrl.value = d.url as string
      }
    },
    onThink: (d) => {
      if (d.content) thinks.value += d.content
    },
    onToken: (t) => {
      generatedHtml.value += t
      convStore.messages[assistantIdx].content += t
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
      convStore.loadConversations(pid)
    },
    onAborted: () => {
      generating.value = false
      finished.value = true
      errorMsg.value = '已取消'
    },
    onError: (m) => {
      generating.value = false
      finished.value = true
      errorMsg.value = m
    },
  }

  esRef.value = startChat({
    model: model.value,
    messages: convStore.messages.map((m) => ({
      role: m.role as 'user' | 'assistant',
      content: m.content,
    })),
    traceId: traceId.value,
    conversationId: cid,
    cb,
  })
}

async function stop() {
  if (!generating.value) return
  generating.value = false
  // 级联取消(C1):通知业务 /api/cancel -> 业务转发 AI /cancel -> Worker 中断生成,
  // 省下后续 token 成本;同时关闭本地 EventSource 不再消费。
  if (traceId.value) await cancelChat(traceId.value)
  esRef.value?.close()
  esRef.value = null
}

async function rate(r: 'up' | 'down') {
  rating.value = r
  if (traceId.value) await sendFeedback(traceId.value, r)
}

onMounted(async () => {
  // 启动顺序:先恢复登录态(/auth/me,未登录返回 null 不阻塞) -> 拉模型列表供选择器
  // -> 加载项目 -> 载入当前项目的首个会话消息。
  await auth.init()
  const m = await fetchModels()
  if (m.length) models.value = m
  await projectStore.load()
  await loadCurrentProject()
})

watch(
  () => projectStore.currentProjectId,
  async (id) => {
    if (id != null) {
      await convStore.loadConversations(id)
      if (convStore.conversations.length)
        await convStore.loadMessages(convStore.conversations[0].id)
      else convStore.messages = []
    }
  },
)

watch(
  () => convStore.pendingConvId,
  async (id) => {
    if (id != null) {
      await convStore.loadMessages(id)
      convStore.pendingConvId = null
    }
  },
)

watch(
  () => auth.user,
  (u) => {
    // 登录成功(thinking:user 从 null 变为有值):关闭登录框;
    // 若此前是"未登录点发送"触发的门禁(pendingSend=true),则自动重发那条消息,
    // 用户无感知地完成"弹窗登录 → 继续对话"的闭环。
    if (u) {
      auth.closeLogin()
      if (pendingSend.value) {
        pendingSend.value = false
        send()
      }
    }
  },
)
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

      <div v-if="stages.length || thinks" class="trail-wrap">
        <ThoughtTrail
          :stages="stages"
          :thinks="thinks"
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
          <span>这次生成质量如何?</span>
          <button :class="{ on: rating === 'up' }" @click="rate('up')">👍</button>
          <button :class="{ on: rating === 'down' }" @click="rate('down')">👎</button>
          <a v-if="previewUrl" :href="previewUrl" target="_blank" rel="noreferrer" class="open">
            打开线上预览 ↗
          </a>
        </div>
      </div>
    </div>

    <div class="right-pane">
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
  max-height: 30%;
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
  gap: 10px;
  font-size: 13px;
  color: var(--muted);
  margin-top: 8px;
}
.feedback button {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 8px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 15px;
}
.feedback button.on {
  border-color: var(--brand2);
  background: #eef2ff;
}
.feedback .open {
  margin-left: auto;
  color: var(--brand);
  text-decoration: none;
  font-weight: 600;
}
</style>
