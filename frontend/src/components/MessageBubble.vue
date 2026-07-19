<script setup lang="ts">
import { ref } from 'vue'
import MarkdownView from './MarkdownView.vue'

defineProps<{ role: string; content: string; time?: string }>()
const expanded = ref(false)
function fmtTime(t: string): string {
  if (!t) return ''
  const d = new Date(t)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="bubble" :class="role">
    <div class="role">{{ role === 'user' ? '你' : 'AI' }}<span v-if="time" class="time">{{ fmtTime(time) }}</span></div>
    <div class="body" :class="{ clamped: !expanded && role === 'assistant' }">
      <MarkdownView v-if="role === 'assistant'" :content="content" />
      <span v-else>{{ content }}</span>
    </div>
    <button v-if="role === 'assistant' && !expanded" class="expand" @click="expanded = true">
      展开全部 ▾
    </button>
  </div>
</template>

<style scoped>
.bubble {
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 14px;
  line-height: 1.6;
}
.bubble.user {
  background: #eef2ff;
  border: 1px solid #e0e7ff;
  align-self: flex-end;
  max-width: 90%;
}
.bubble.assistant {
  background: #fff;
  border: 1px solid var(--border);
  width: 100%;
}
.body.clamped {
  max-height: 60vh;
  overflow: auto;
}
.expand {
  margin-top: 6px;
  font-size: 12px;
  color: var(--brand);
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px 8px;
  background: #fff;
}
.time { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }
</style>
