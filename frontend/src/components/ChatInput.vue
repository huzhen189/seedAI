<script setup lang="ts">
import ModelSelector from './ModelSelector.vue'
import type { ModelInfo } from '../types'

defineProps<{
  model: string
  generating: boolean
  cancelling: boolean
  models: ModelInfo[]
}>()

const emit = defineEmits<{
  'update:model': [string]
  send: []
  stop: []
}>()

const value = defineModel<string>('value', { default: '' })

function submit() {
  if (!value.value.trim()) return
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
    </div>
    <div class="row">
      <textarea
        v-model="value"
        rows="2"
        placeholder="描述你想生成的网站，AI 会先规划再产出…"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <button v-if="!generating" class="send" data-track="发送" :disabled="!value.trim()" @click="submit">
        发送 ⏎
      </button>
      <button v-else-if="cancelling" class="stop" disabled>暂停中…</button>
      <template v-else>
        <button class="stop" data-track="停止" @click="emit('stop')">停止</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.input-wrap { display: flex; flex-direction: column; gap: 8px; }
.bar { display: flex; align-items: center; gap: 8px; }
.row { display: flex; gap: 8px; align-items: flex-end; }
textarea { flex: 1; resize: none; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; font-family: inherit; font-size: 14px; }
.send, .stop { border: none; border-radius: var(--radius-md); padding: 10px 18px; font-weight: 600; cursor: pointer; color: #fff; transition: transform var(--transition-fast), box-shadow var(--transition-fast), filter var(--transition-fast); }
.send { background: var(--brand-grad); box-shadow: 0 6px 16px rgba(21,196,164,.28); }
.send:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 10px 22px rgba(21,196,164,.36); filter: brightness(1.03); }
.send:active:not(:disabled) { transform: translateY(0); }
.send:disabled { opacity: 0.5; cursor: not-allowed; box-shadow: none; }
.stop { background: var(--err); }
.stop:hover { transform: translateY(-2px); }
</style>
