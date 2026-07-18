<script setup lang="ts">
import { onMounted, ref } from 'vue'
import ModelSelector from '../components/ModelSelector.vue'
import ThoughtTrail from '../components/ThoughtTrail.vue'
import PreviewPane from '../components/PreviewPane.vue'
import ChatInput from '../components/ChatInput.vue'
import {
  startChat,
  cancelChat,
  fetchModels,
  sendFeedback,
  type ChatCallbacks,
} from '../api/chat'
import type { ChatMessage, ModelInfo } from '../types'

const models = ref<ModelInfo[]>([])
const model = ref('hy3')
const input = ref('')
const generating = ref(false)
const finished = ref(false)

const conversation = ref<ChatMessage[]>([]) // 多轮用户消息
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

function genTraceId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 't' + Date.now().toString(16) + Math.random().toString(16).slice(2)
}

function pushStage(stage: string) {
  currentStage.value = stage
  if (stage && !stages.value.includes(stage)) stages.value.push(stage)
}

async function send() {
  const text = input.value.trim()
  if (!text || generating.value) return

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

  conversation.value.push({ role: 'user', content: text })
  const messages: ChatMessage[] = conversation.value.slice()

  generating.value = true
  input.value = ''

  const cb: ChatCallbacks = {
    onNode: (d) => {
      if (d.stage) {
        pushStage(d.stage)
        // 预览事件:优先用线上直链,否则依赖已累积 token(srcdoc)
        if (d.stage === 'preview' && d.url) previewUrl.value = d.url as string
      }
    },
    onThink: (d) => {
      if (d.content) thinks.value += d.content
    },
    onToken: (t) => {
      generatedHtml.value += t
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

  esRef.value = startChat({ model: model.value, messages, traceId: traceId.value, cb })
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
  const m = await fetchModels()
  if (m.length) models.value = m
})
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">SeedAI · 建站助手</div>
      <div class="right">
        <span class="anon">匿名体验</span>
        <ModelSelector :models="models" v-model:model="model" />
      </div>
    </header>

    <main class="body">
      <!-- 左:对话 + 思考流 -->
      <section class="left">
        <div class="conv">
          <div v-if="conversation.length === 0" class="empty">
            在下方描述你想生成的网站,AI 会先规划需求,再流式产出单文件 HTML 并实时预览。
          </div>
          <div v-for="(m, i) in conversation" :key="i" class="bubble user">
            <div class="role">你</div>
            <div class="text">{{ m.content }}</div>
          </div>
        </div>
        <div class="trail-wrap">
          <ThoughtTrail
            :stages="stages"
            :thinks="thinks"
            :degraded="degraded"
            :current="currentStage"
          />
        </div>
      </section>

      <!-- 右:实时预览 -->
      <section class="right-pane">
        <PreviewPane :html="generatedHtml" :url="previewUrl" :loading="generating" />
        <div v-if="errorMsg" class="error">⚠ {{ errorMsg }}</div>
        <div v-if="finished && !errorMsg && (generatedHtml || previewUrl)" class="feedback">
          <span>这次生成质量如何?</span>
          <button :class="{ on: rating === 'up' }" @click="rate('up')">👍</button>
          <button :class="{ on: rating === 'down' }" @click="rate('down')">👎</button>
          <a v-if="previewUrl" :href="previewUrl" target="_blank" rel="noreferrer" class="open">
            打开线上预览 ↗
          </a>
        </div>
      </section>
    </main>

    <footer class="footer">
      <ChatInput v-model:value="input" :generating="generating" @send="send" @stop="stop" />
    </footer>
  </div>
</template>

<style scoped>
.app {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.brand {
  font-weight: 700;
  font-size: 16px;
  color: var(--brand);
}
.right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.anon {
  font-size: 12px;
  color: var(--muted);
  border: 1px solid var(--border);
  padding: 3px 8px;
  border-radius: 999px;
}
.body {
  flex: 1;
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 16px;
  padding: 16px;
  min-height: 0;
}
.left {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}
.conv {
  flex: 0 0 auto;
  max-height: 38%;
  overflow: auto;
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
.bubble {
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 14px;
  line-height: 1.6;
}
.bubble.user {
  background: #eef2ff;
  border: 1px solid #e0e7ff;
}
.bubble .role {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.trail-wrap {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
}
.right-pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
.right-pane > .preview,
.right-pane :deep(.preview) {
  flex: 1;
  min-height: 0;
}
.error {
  color: var(--err);
  font-size: 13px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 8px 12px;
}
.feedback {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--muted);
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
.footer {
  padding: 12px 16px;
  background: var(--panel);
  border-top: 1px solid var(--border);
}
</style>
