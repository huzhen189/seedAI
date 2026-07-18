<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import ThoughtTrail from '../components/ThoughtTrail.vue'
import PreviewPane from '../components/PreviewPane.vue'
import ChatInput from '../components/ChatInput.vue'
import MessageBubble from '../components/MessageBubble.vue'
import SettingsPanel from '../components/SettingsPanel.vue'
import { startChat, cancelChat, fetchModels, sendFeedback, type ChatCallbacks } from '../api/chat'
import { useAuth } from '../composables/useAuth'
import { useProjectStore } from '../stores/project'
import { useConversationStore } from '../stores/conversation'
import type { ModelInfo } from '../types'

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

const showSettings = ref(false)
const pendingSend = ref(false)

const auth = useAuth()
const projectStore = useProjectStore()
const convStore = useConversationStore()

const messages = computed(() => convStore.messages)
const currentProjectName = computed(
  () => projectStore.projects.find((p) => p.id === projectStore.currentProjectId)?.name || '未选择项目',
)

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
  if (convStore.currentConvId == null) {
    await convStore.create(pid, text.slice(0, 20))
  }

  // 重置本次生成状态
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
  if (traceId.value) await cancelChat(traceId.value)
  esRef.value?.close()
  esRef.value = null
}

async function rate(r: 'up' | 'down') {
  rating.value = r
  if (traceId.value) await sendFeedback(traceId.value, r)
}

onMounted(async () => {
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
      if (convStore.conversations.length) await convStore.loadMessages(convStore.conversations[0].id)
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
        <MessageBubble
          v-for="(m, i) in messages"
          :key="i"
          :role="m.role"
          :content="m.content"
        />
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
          @open-settings="showSettings = true"
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

    <SettingsPanel v-if="showSettings" @close="showSettings = false" />
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
