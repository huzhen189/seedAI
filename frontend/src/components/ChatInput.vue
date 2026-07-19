<script setup lang="ts">
import ModelSelector from './ModelSelector.vue'
import type { ModelInfo } from '../types'

defineProps<{
  model: string
  generating: boolean
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
      <button v-if="!generating" class="send" :disabled="!value.trim()" @click="submit">
        发送 ⏎
      </button>
      <button v-else class="stop" @click="emit('stop')">停止</button>
    </div>
  </div>
</template>

<style scoped>
.input-wrap { display: flex; flex-direction: column; gap: 8px; }
.bar { display: flex; align-items: center; gap: 8px; }
.row { display: flex; gap: 8px; align-items: flex-end; }
textarea { flex: 1; resize: none; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; font-family: inherit; font-size: 14px; }
.send, .stop { border: none; border-radius: 10px; padding: 10px 18px; font-weight: 600; cursor: pointer; color: #fff; }
.send { background: var(--brand); }
.send:disabled { opacity: 0.5; cursor: not-allowed; }
.stop { background: var(--err); }
</style>
