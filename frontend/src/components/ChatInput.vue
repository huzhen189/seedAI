<script setup lang="ts">
defineProps<{ generating: boolean; value: string }>()
defineEmits<{
  (e: 'update:value', v: string): void
  (e: 'send'): void
  (e: 'stop'): void
}>()
</script>

<template>
  <div class="input-bar">
    <textarea
      :value="value"
      :disabled="generating"
      @input="$emit('update:value', ($event.target as HTMLTextAreaElement).value)"
      @keydown.enter.exact.prevent="$emit('send')"
      placeholder="描述你想生成的网站,例如:一个个人摄影作品集首页,深色背景、网格布局、鼠标悬停放大"
      rows="3"
    ></textarea>
    <div class="actions">
      <button v-if="!generating" class="send" @click="$emit('send')">生成 ⏎</button>
      <button v-else class="stop" @click="$emit('stop')">停止 ■</button>
    </div>
  </div>
</template>

<style scoped>
.input-bar {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
textarea {
  flex: 1;
  resize: none;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  font: inherit;
  font-size: 14px;
  line-height: 1.5;
  background: var(--panel);
  color: var(--text);
}
textarea:focus {
  outline: 2px solid var(--brand2);
}
.actions {
  display: flex;
}
.send,
.stop {
  padding: 10px 18px;
  border: 0;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  color: #fff;
}
.send {
  background: var(--brand);
}
.send:hover {
  background: var(--brand2);
}
.stop {
  background: var(--err);
}
</style>
