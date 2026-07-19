<script setup lang="ts">
import type { ModelInfo } from '../types'

defineProps<{ models: ModelInfo[]; model: string }>()
defineEmits<{ (e: 'update:model', v: string): void }>()
</script>

<template>
  <select
    class="model-select"
    :value="model"
    @change="$emit('update:model', ($event.target as HTMLSelectElement).value)"
  >
    <option v-if="models.length === 0" value="hy3">HY3(默认)</option>
    <option
      v-for="m in models"
      :key="m.id"
      :value="m.id"
      :title="m.desc || m.label"
    >
      {{ m.label }} {{ m.version ? '·' + m.version : '' }} {{ m.speed ? '| ' + m.speed : '' }}
    </option>
  </select>
</template>

<style scoped>
.model-select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  color: var(--text);
  font-size: 14px;
  cursor: pointer;
  max-width: 260px;
}
.model-select:focus {
  outline: 2px solid var(--brand2);
}
</style>
