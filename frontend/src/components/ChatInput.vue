<script setup lang="ts">
import { ref } from 'vue'
import ModelSelector from './ModelSelector.vue'
import type { ModelInfo } from '../types'

const props = defineProps<{
  model: string
  generating: boolean
  models: ModelInfo[]
}>()

const emit = defineEmits<{
  'update:model': [string]
  send: []
  stop: []
  'open-settings': []
}>()

const value = defineModel<string>('value', { default: '' })
const fileInput = ref<HTMLInputElement | null>(null)
const attachments = ref<File[]>([])

function onPick() {
  fileInput.value?.click()
}
function onFiles(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (files) attachments.value.push(...Array.from(files))
  ;(e.target as HTMLInputElement).value = ''
}
function removeAtt(i: number) {
  attachments.value.splice(i, 1)
}
function submit() {
  if (!value.value.trim() || props.generating) return
  emit('send')
}
</script>

<template>
  <div class="input-wrap">
    <div class="bar">
      <ModelSelector
        :models="models"
        :model="model"
        @update:model="(m: string) => emit('update:model', m)"
      />
      <button class="icon" title="附件（上传后端待接入）" @click="onPick">📎</button>
      <button class="icon" title="对话设置" @click="emit('open-settings')">⚙️</button>
      <input ref="fileInput" type="file" multiple hidden @change="onFiles" />
    </div>
    <div v-if="attachments.length" class="atts">
      <span v-for="(f, i) in attachments" :key="i" class="chip">
        {{ f.name }} <b @click="removeAtt(i)">×</b>
      </span>
    </div>
    <div class="row">
      <textarea
        v-model="value"
        rows="2"
        placeholder="描述你想生成的网站，AI 会先规划再产出…"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <button v-if="!generating" class="send" :disabled="!value.trim()" @click="submit">
        发送 ⏎
      </button>
      <button v-else class="stop" @click="emit('stop')">停止</button>
    </div>
  </div>
</template>

<style scoped>
.input-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.icon {
  border: 1px solid var(--border);
  background: var(--panel);
  border-radius: 8px;
  padding: 4px 8px;
  cursor: pointer;
  font-size: 14px;
}
.atts {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.chip {
  background: #eef2ff;
  border: 1px solid #e0e7ff;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 12px;
}
.chip b {
  cursor: pointer;
  margin-left: 4px;
}
.row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
textarea {
  flex: 1;
  resize: none;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  font-family: inherit;
  font-size: 14px;
}
.send,
.stop {
  border: none;
  border-radius: 10px;
  padding: 10px 18px;
  font-weight: 600;
  cursor: pointer;
  color: #fff;
}
.send {
  background: var(--brand);
}
.send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.stop {
  background: var(--err);
}
</style>
