<script setup lang="ts">
defineProps<{ html: string; url: string | null; loading: boolean }>()
</script>

<template>
  <div class="preview">
    <div v-if="loading && !html && !url" class="placeholder">
      <div class="spinner"></div>
      <span>等待生成…</span>
    </div>
    <iframe
      v-else
      class="frame"
      :src="url || undefined"
      :srcdoc="url ? undefined : html"
      sandbox="allow-scripts allow-same-origin allow-forms"
      title="site-preview"
    ></iframe>
  </div>
</template>

<style scoped>
.preview {
  height: 100%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  display: flex;
}
.frame {
  width: 100%;
  height: 100%;
  border: 0;
  background: #fff;
}
.placeholder {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: var(--muted);
}
.spinner {
  width: 28px;
  height: 28px;
  border: 3px solid var(--border);
  border-top-color: var(--brand);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
