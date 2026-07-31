<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import MarkdownView from './MarkdownView.vue'
import type { ContentData, QcResult, RatingDims, QcDimension } from '../types'
import { QC_DIMENSIONS, QC_DIM_LABELS } from '../types'

const props = withDefaults(
  defineProps<{
    role: string
    content: string
    time?: string
    traceId?: string
    /** 后置 QC 单裁判结果(来自 SSE `qc` 事件) */
    qc?: QcResult | null
    /** 当前用户已提交的评分(1-10), 缺省 null=未评价 */
    myRating?: number | null
    /** 当前用户已提交的 6 维细分 */
    myDims?: RatingDims | null
    myComment?: string | null
    /** 是否允许评价(已登录可评) */
    canRate?: boolean
    /** 是否正在流式生成(逐字 token 推送中)。流式阶段不进 MarkdownView,
     *  直接渲染原始文本, 避免每 token 整篇重解析+全量语法高亮导致卡死(O(n²))。 */
    streaming?: boolean
  }>(),
  { qc: null, myRating: null, myDims: null, myComment: null, canRate: true },
)

const emit = defineEmits<{
  (e: 'rate', p: { rating: number; comment: string; dimensions: RatingDims }): void
  /** 点击建站产物里的某个文件: 通知父组件联动右侧预览面板 */
  (e: 'open-file', name: string): void
}>()

const expanded = ref(false)
const showQc = ref(false)
const editing = ref(false)
const expandedDims = ref(false)

// 评价编辑态
const overall = ref(0)
const dims = reactive<RatingDims>({})
const comment = ref('')

function startEdit() {
  overall.value = props.myRating ?? 0
  for (const d of QC_DIMENSIONS) delete dims[d]
  if (props.myDims) Object.assign(dims, props.myDims)
  comment.value = props.myComment ?? ''
  expandedDims.value = !!(props.myDims && Object.keys(props.myDims).length)
  editing.value = true
}

function cancelEdit() {
  editing.value = false
}

function submitRate() {
  if (overall.value < 1) return
  // 仅保留用户实际打过的维度
  const sel: RatingDims = {}
  for (const d of QC_DIMENSIONS) {
    if (typeof dims[d] === 'number' && dims[d]! > 0) sel[d] = dims[d]
  }
  emit('rate', { rating: overall.value, comment: comment.value.trim(), dimensions: sel })
  editing.value = false
}

function starColor(v: number): string {
  if (v >= 8) return '#16a34a'
  if (v >= 6) return '#d97706'
  if (v > 0) return '#dc2626'
  return 'var(--muted)'
}

function parseContent(c: string): ContentData {
  if (c.startsWith('{') && c.includes('"type"')) {
    try {
      const obj = JSON.parse(c)
      if (obj && obj.type) return obj as ContentData
    } catch { /* ignore */ }
  }
  return { type: 'plain', text: c }
}

const parsed = computed(() => parseContent(props.content))

const isExpandable = computed(() =>
  parsed.value.type === 'plain' && parsed.value.text.length > 2000,
)

const STAGE_LABELS: Record<string, string> = {
  enter_router: '识别需求类型',
  dispatch: '加载AI能力',
  enter_planner: '制定方案',
  enter_coder: '生成代码',
  enter_reviewer: '评审校验',
  previewing: '上传预览',
  preview: '预览完成',
  done: '完成',
}

function fmtTime(t: string): string {
  if (!t) return ''
  const d = new Date(t)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="bubble" :class="role">
    <div class="role">
      {{ role === 'user' ? '你' : 'AI' }}
      <span v-if="time" class="time">{{ fmtTime(time) }}</span>
    </div>

    <div class="body" :class="{ expanded: expanded }">
      <!-- 纯文本 / 闲聊 / 建站文字总结(A#485: 去 site-card 双卡, 只留总结文案) -->
      <!-- 流式生成中: 直接渲染原始文本(保留换行), 跳过 MarkdownView 的逐字重解析+高亮(防卡死)。
           生成完成(streaming=false)后才一次性交给 MarkdownView 渲染, 性能从 O(n²) 降到 O(n)。 -->
      <div v-if="parsed.type === 'plain' && role === 'assistant' && streaming" class="raw-stream">{{ parsed.text }}</div>
      <MarkdownView v-else-if="parsed.type === 'plain' && role === 'assistant'" :content="parsed.text" />
      <span v-else-if="parsed.type === 'plain'">{{ parsed.text }}</span>
      <!-- 代码产物 -->
      <div v-else-if="parsed.type === 'code'" class="code-card">
        <div class="code-title">📄 {{ parsed.title }}</div>
        <pre v-if="parsed.code_preview" class="code-preview"><code>{{ parsed.code_preview }}</code></pre>
      </div>
      <!-- 错误消息 -->
      <div v-else-if="parsed.type === 'error'" class="error-card">⚠️ {{ parsed.message }}</div>
      <!-- trail 思考过程 -->
      <div v-else-if="parsed.type === 'trail'" class="trail-card">
        <div v-for="(evt, i) in parsed.events" :key="i" class="trail-event">
          <span class="trail-badge">{{ evt.event === 'node' ? '●' : evt.event === 'think' ? '💭' : evt.event === 'plan' ? '📋' : '🔍' }}</span>
          <span v-if="evt.event === 'node'">{{ STAGE_LABELS[evt.data?.stage] || evt.data?.stage }}</span>
          <span v-else-if="evt.event === 'think'">{{ evt.data?.content?.slice(0, 200) }}</span>
          <span v-else-if="evt.event === 'plan'">{{ evt.data?.title }}</span>
          <span v-else-if="evt.event === 'intent'">{{ evt.data?.level1 }}/{{ evt.data?.level2 }}</span>
        </div>
      </div>
      <!-- 兜底 -->
      <span v-else>{{ content }}</span>
    </div>

    <!-- ===== 气泡内嵌执行轨迹 / 二次确认详情(P1/P2): 由 ChatView 通过 #trail 注入 ===== -->
    <slot name="trail" />

    <button v-if="isExpandable && !expanded" class="expand" @click="expanded = true">展开全部 ▾</button>
    <button v-if="isExpandable && expanded" class="expand" @click="expanded = false">收起 ▲</button>

    <!-- ===== 后置 QC 徽标(assistant 且仅当存在 QC 结果) ===== -->
    <div v-if="role === 'assistant' && qc" class="qc-footer">
      <button class="qc-chip" :style="{ '--qc': starColor(qc.overall) }" @click="showQc = !showQc">
        🛡️ 质检 {{ qc.overall.toFixed(1) }}
        <span v-if="qc.needs_review" class="qc-flag">需复核</span>
        <span v-if="qc.partial" class="qc-flag gray" title="部分裁判失败/超时">部分</span>
      </button>
      <div v-if="showQc" class="qc-detail">
        <div v-for="d in QC_DIMENSIONS" :key="d" class="qc-dim">
          <span class="qc-dim-label">{{ QC_DIM_LABELS[d as QcDimension] }}</span>
          <span class="qc-bar"><i :style="{ width: ((qc.dimensions?.[d]?.mean ?? 0) * 10) + '%', background: starColor(qc.dimensions?.[d]?.mean ?? 0) }"></i></span>
          <span class="qc-mean" :style="{ color: starColor(qc.dimensions?.[d]?.mean ?? 0) }">{{ (qc.dimensions?.[d]?.mean ?? 0).toFixed(1) }}</span>
          <span class="qc-judges">
            <i
              v-for="(s, i) in (qc.dimensions?.[d]?.scores ?? [])"
              :key="i"
              class="qc-dot"
              :class="{ zero: !s }"
              :title="['deepseek','qwen','hy3'][i] + ': ' + (s || '—')"
            >{{ s || '–' }}</i>
          </span>
        </div>
        <div v-if="qc.safety_risk && qc.safety_risk !== 'low'" class="qc-risk">
          安全风险: {{ qc.safety_risk }}
        </div>
      </div>
    </div>

    <!-- ===== 气泡内多维度评价(assistant) ===== -->
    <div v-if="role === 'assistant' && canRate" class="rate-footer">
      <!-- 未评价且未编辑: 显示入口 -->
      <button v-if="myRating == null && !editing" class="rate-btn" @click="startEdit">⭐ 评价</button>
      <!-- 已评价且未编辑: 显示已评 + 修改 -->
      <div v-else-if="myRating != null && !editing" class="rated">
        <span class="rated-label">我的评分</span>
        <span class="stars ro">
          <i v-for="i in 10" :key="i" class="star" :class="{ on: i <= (myRating as number) }">★</i>
        </span>
        <button class="rate-edit" @click="startEdit">修改</button>
      </div>

      <!-- 编辑面板 -->
      <div v-if="editing" class="rate-panel">
        <div class="rate-row">
          <span class="rate-row-label">总体</span>
          <span class="stars">
            <i v-for="i in 10" :key="i" class="star" :class="{ on: i <= overall }" @click="overall = i">★</i>
          </span>
          <span class="rate-hint">{{ overall || '未评' }}</span>
        </div>

        <button class="rate-toggle" @click="expandedDims = !expandedDims">
          {{ expandedDims ? '收起多维度 ▴' : '展开多维度评价 ▾' }}
        </button>

        <div v-if="expandedDims" class="rate-dims">
          <div v-for="d in QC_DIMENSIONS" :key="d" class="rate-row">
            <span class="rate-row-label">{{ QC_DIM_LABELS[d as QcDimension] }}</span>
            <span class="stars">
              <i
                v-for="i in 10"
                :key="i"
                class="star"
                :class="{ on: i <= (dims[d as QcDimension] || 0) }"
                @click="dims[d as QcDimension] = i"
              >★</i>
            </span>
          </div>
        </div>

        <textarea
          v-model="comment"
          class="rate-comment"
          rows="2"
          placeholder="补充说明(可选)…"
        ></textarea>

        <div class="rate-actions">
          <button class="rate-submit" :disabled="overall < 1" @click="submitRate">提交</button>
          <button class="rate-cancel" @click="cancelEdit">取消</button>
        </div>
      </div>
    </div>
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
  background: var(--brand-bg);
  border: 1px solid #cdeee7;
  align-self: flex-end;
  max-width: 90%;
}
.bubble.assistant {
  background: var(--surface-2);
  border: 1px solid var(--border);
  max-width: 100%;
}
.body { max-height: 50vh; overflow-y: auto; }
.body.expanded { max-height: none; overflow-y: visible; }
/* 流式阶段的原始文本: 轻量"正在输出"反馈, 不做 Markdown/高亮。
   限高+弱化, 不喧宾夺主(用户不关心逐字原文, 关心最终富文本); 高度超限可滚动。 */
.raw-stream {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.5;
  margin: 0;
  max-height: 140px;
  overflow-y: auto;
  color: var(--text-4);
  background: var(--surface-3);
  border: 1px solid var(--border, var(--surface-3));
  border-radius: 8px;
  padding: 8px 10px;
}
.expand {
  margin-top: 6px; font-size: 12px; color: var(--brand); cursor: pointer;
  border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; background: var(--surface-2);
}
.time { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }

/* ---- Code Card ---- */
.code-card { background: #1e1e1e; border-radius: 8px; padding: 10px; }
.code-title { color: #ccc; font-size: 13px; margin-bottom: 6px; }
.code-preview { max-height: 300px; overflow: auto; margin: 0; }
.code-preview code { color: #d4d4d4; font-size: 12px; }

/* ---- Error Card ---- */
.error-card {
  background: var(--err-bg); border: 1px solid var(--err-border);
  border-radius: 8px; padding: 8px 12px; color: var(--err); font-size: 13px;
}

/* ---- Trail Card ---- */
.trail-card { padding: 4px 0; }
.trail-event {
  display: flex; align-items: flex-start; gap: 6px; padding: 3px 0;
  font-size: 12px; color: var(--text-4);
}
.trail-badge { flex-shrink: 0; width: 18px; text-align: center; font-size: 10px; }

/* ---- QC 徽标 ---- */
.qc-footer { margin-top: 8px; border-top: 1px dashed var(--border); padding-top: 6px; }
.qc-chip {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; cursor: pointer;
  border: 1px solid var(--qc, var(--border)); color: var(--qc, var(--brand));
  background: color-mix(in srgb, var(--qc, #15c4a4) 8%, #fff);
  border-radius: 999px; padding: 2px 10px; font-weight: 600;
}
.qc-flag {
  font-size: 10px; background: #f59e0b; color: #fff; border-radius: 4px; padding: 0 5px; font-weight: 700;
}
.qc-flag.gray { background: #9baba1; }
.qc-detail {
  margin-top: 8px; background: var(--surface-3); border: 1px solid var(--surface-3); border-radius: 10px; padding: 8px 10px;
}
.qc-dim {
  display: grid; grid-template-columns: 56px 1fr 30px 64px; align-items: center; gap: 8px;
  font-size: 12px; padding: 3px 0;
}
.qc-dim-label { color: var(--text-3); }
.qc-bar { height: 6px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }
.qc-bar > i { display: block; height: 100%; border-radius: 3px; }
.qc-mean { font-weight: 700; text-align: right; }
.qc-judges { display: flex; gap: 3px; justify-content: flex-end; }
.qc-dot {
  width: 18px; height: 18px; line-height: 18px; text-align: center; font-size: 10px;
  background: var(--surface-3); border-radius: 4px; color: var(--text-3); font-style: normal;
}
.qc-dot.zero { color: var(--text-3); background: var(--surface-3); }
.qc-risk { margin-top: 6px; font-size: 11px; color: var(--err); font-weight: 600; }

/* ---- 评价 ---- */
.rate-footer { margin-top: 8px; border-top: 1px dashed var(--border); padding-top: 6px; }
.rate-btn {
  font-size: 12px; cursor: pointer; border: 1px solid var(--brand); color: var(--brand);
  background: var(--surface-2); border-radius: 999px; padding: 2px 12px; font-weight: 600;
}
.rate-btn:hover { background: color-mix(in srgb, var(--brand) 8%, #fff); }
.rated { display: flex; align-items: center; gap: 8px; }
.rated-label { font-size: 12px; color: var(--muted); }
.rate-edit {
  font-size: 11px; cursor: pointer; border: 1px solid var(--border); background: var(--surface-2);
  border-radius: 6px; padding: 1px 8px; color: var(--brand); margin-left: auto;
}
.rate-panel {
  margin-top: 8px; background: var(--surface-3); border: 1px solid var(--surface-3); border-radius: 10px; padding: 10px;
}
.rate-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.rate-row-label { width: 56px; font-size: 12px; color: var(--text-3); flex-shrink: 0; }
.stars { display: inline-flex; gap: 1px; }
.stars .star {
  font-size: 14px; color: var(--text-3); cursor: pointer; transition: color .12s, transform .12s;
  font-style: normal; line-height: 1;
}
.stars .star.on { color: #f59e0b; }
.stars .star:hover { transform: scale(1.15); }
.stars.ro .star { cursor: default; }
.stars.ro .star:hover { transform: none; }
.rate-hint { font-size: 11px; color: var(--muted); margin-left: 4px; }
.rate-toggle {
  margin: 6px 0; font-size: 11px; cursor: pointer; border: none; background: none;
  color: var(--brand); padding: 0;
}
.rate-dims {
  background: var(--surface-2); border: 1px solid var(--surface-3); border-radius: 8px; padding: 4px 8px; margin-bottom: 6px;
}
.rate-comment {
  width: 100%; box-sizing: border-box; font-size: 12px; border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 6px; resize: vertical; font-family: inherit;
}
.rate-actions { display: flex; gap: 8px; margin-top: 6px; }
.rate-submit {
  font-size: 12px; cursor: pointer; border: none; background: var(--brand, #15c4a4); color: #fff;
  border-radius: 6px; padding: 4px 14px; font-weight: 600;
}
.rate-submit:disabled { opacity: 0.5; cursor: not-allowed; }
.rate-cancel {
  font-size: 12px; cursor: pointer; border: 1px solid var(--border); background: var(--surface-2);
  border-radius: 6px; padding: 4px 12px; color: var(--muted);
}
</style>
