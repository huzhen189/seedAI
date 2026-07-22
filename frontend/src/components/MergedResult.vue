<script setup lang="ts">
import type { FailedSubTask } from '../types'

defineProps<{
  /** 成功完成的子任务数 */
  successCount: number
  /** 未完成的子任务数(拦截 / 跳过 / 失败) */
  failCount: number
  /** 部分失败清单(兜底 4: 部分失败交付) */
  failedTasks: FailedSubTask[]
}>()
</script>

<template>
  <div class="merged" :class="{ partial: failCount > 0 }">
    <div class="merged-head">
      <span class="merged-icon">✅</span>
      <span class="merged-title">多意图结果已合并</span>
      <span class="merged-counts">
        <span class="ok">{{ successCount }} 完成</span>
        <span v-if="failCount > 0" class="bad">· {{ failCount }} 未完成</span>
      </span>
    </div>
    <div v-if="failCount > 0" class="partial-list">
      <div class="partial-title">⚠ 以下子任务未执行，其余结果已正常交付：</div>
      <div v-for="(f, i) in failedTasks" :key="i" class="partial-item">
        <span class="pid">{{ f.id }}</span>
        <span class="pgoal">{{ f.goal }}</span>
        <span class="preason">{{ f.error }}</span>
      </div>
    </div>
    <div v-else class="all-ok">所有子任务均已成功执行，已合并为一条完整回复。</div>
  </div>
</template>

<style scoped>
.merged {
  border: 1px solid var(--ok);
  background: linear-gradient(180deg, #f0fdf4 0%, #ffffff 100%);
  border-radius: 12px;
  padding: 10px 14px;
  margin-top: 10px;
}
.merged.partial { border-color: var(--warn); background: linear-gradient(180deg, #fffbeb 0%, #ffffff 100%); }
.merged-head { display: flex; align-items: center; gap: 8px; }
.merged-icon { font-size: 16px; }
.merged-title { font-weight: 700; font-size: 13.5px; color: #15803d; }
.merged.partial .merged-title { color: var(--warn); }
.merged-counts { font-size: 12px; margin-left: auto; color: var(--muted); }
.merged-counts .ok { color: #15803d; font-weight: 600; }
.merged-counts .bad { color: var(--warn); font-weight: 600; }
.partial-list { margin-top: 8px; }
.partial-title { font-size: 12px; color: var(--warn); margin-bottom: 4px; }
.partial-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  padding: 4px 8px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 8px;
  margin-bottom: 4px;
}
.pid { font-weight: 700; color: #b45309; flex: none; }
.pgoal { flex: 1; min-width: 0; color: #334155; }
.preason { flex: none; color: #b91c1c; max-width: 50%; text-align: right; }
.all-ok { font-size: 12px; color: #15803d; margin-top: 6px; }
</style>
