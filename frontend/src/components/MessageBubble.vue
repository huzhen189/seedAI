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
    /** 后置 QC 三裁判结果(来自 SSE `qc` 事件) */
    qc?: QcResult | null
    /** 当前用户已提交的评分(1-10), 缺省 null=未评价 */
    myRating?: number | null
    /** 当前用户已提交的 6 维细分 */
    myDims?: RatingDims | null
    myComment?: string | null
    /** 是否允许评价(已登录可评) */
    canRate?: boolean
  }>(),
  { qc: null, myRating: null, myDims: null, myComment: null, canRate: true },
)

const emit = defineEmits<{
  (e: 'rate', p: { rating: number; comment: string; dimensions: RatingDims }): void
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
      <!-- 纯文本 / 闲聊 -->
      <MarkdownView v-if="parsed.type === 'plain' && role === 'assistant'" :content="parsed.text" />
      <span v-else-if="parsed.type === 'plain'">{{ parsed.text }}</span>
      <!-- 建站产物 -->
      <div v-else-if="parsed.type === 'site'" class="site-card">
        <div class="site-title">🌐 {{ parsed.title }}</div>
        <div class="site-links">
          <a :href="parsed.preview_url" target="_blank" class="btn">🔗 预览</a>
          <a v-if="parsed.download_url" :href="parsed.download_url" class="btn">📥 下载</a>
        </div>
        <div v-if="parsed.files?.length" class="site-files">
          <span v-for="f in parsed.files" :key="f.name" class="file-tag">{{ f.name }} ({{ (f.size / 1024).toFixed(1) }}KB)</span>
        </div>
      </div>
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
          <span class="qc-bar"><i :style="{ width: (qc.dimensions[d].mean * 10) + '%', background: starColor(qc.dimensions[d].mean) }"></i></span>
          <span class="qc-mean" :style="{ color: starColor(qc.dimensions[d].mean) }">{{ qc.dimensions[d].mean.toFixed(1) }}</span>
          <span class="qc-judges">
            <i
              v-for="(s, i) in qc.dimensions[d].scores"
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
  background: #eef2ff;
  border: 1px solid #e0e7ff;
  align-self: flex-end;
  max-width: 90%;
}
.bubble.assistant {
  background: #fff;
  border: 1px solid var(--border);
  max-width: 100%;
}
.body { max-height: 50vh; overflow-y: auto; }
.body.expanded { max-height: none; overflow-y: visible; }
.expand {
  margin-top: 6px; font-size: 12px; color: var(--brand); cursor: pointer;
  border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; background: #fff;
}
.time { font-size: 11px; color: var(--muted); font-weight: 400; margin-left: 6px; }

/* ---- Site Card ---- */
.site-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 12px;
}
.site-title { font-weight: 600; font-size: 15px; margin-bottom: 8px; }
.site-links { display: flex; gap: 8px; margin-bottom: 8px; }
.site-links .btn {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 12px; font-size: 13px; border-radius: 6px;
  background: var(--brand, #4f46e5); color: #fff; text-decoration: none;
}
.site-links .btn:hover { opacity: 0.9; }
.site-files { display: flex; flex-wrap: wrap; gap: 4px; }
.file-tag {
  font-size: 11px; background: #e2e8f0; padding: 2px 6px; border-radius: 4px;
  color: #475569;
}

/* ---- Code Card ---- */
.code-card { background: #1e1e1e; border-radius: 8px; padding: 10px; }
.code-title { color: #ccc; font-size: 13px; margin-bottom: 6px; }
.code-preview { max-height: 300px; overflow: auto; margin: 0; }
.code-preview code { color: #d4d4d4; font-size: 12px; }

/* ---- Error Card ---- */
.error-card {
  background: #fef2f2; border: 1px solid #fecaca;
  border-radius: 8px; padding: 8px 12px; color: #b91c1c; font-size: 13px;
}

/* ---- Trail Card ---- */
.trail-card { padding: 4px 0; }
.trail-event {
  display: flex; align-items: flex-start; gap: 6px; padding: 3px 0;
  font-size: 12px; color: #64748b;
}
.trail-badge { flex-shrink: 0; width: 18px; text-align: center; font-size: 10px; }

/* ---- QC 徽标 ---- */
.qc-footer { margin-top: 8px; border-top: 1px dashed var(--border); padding-top: 6px; }
.qc-chip {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; cursor: pointer;
  border: 1px solid var(--qc, var(--border)); color: var(--qc, var(--brand));
  background: color-mix(in srgb, var(--qc, #4f46e5) 8%, #fff);
  border-radius: 999px; padding: 2px 10px; font-weight: 600;
}
.qc-flag {
  font-size: 10px; background: #f59e0b; color: #fff; border-radius: 4px; padding: 0 5px; font-weight: 700;
}
.qc-flag.gray { background: #94a3b8; }
.qc-detail {
  margin-top: 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 8px 10px;
}
.qc-dim {
  display: grid; grid-template-columns: 56px 1fr 30px 64px; align-items: center; gap: 8px;
  font-size: 12px; padding: 3px 0;
}
.qc-dim-label { color: #475569; }
.qc-bar { height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
.qc-bar > i { display: block; height: 100%; border-radius: 3px; }
.qc-mean { font-weight: 700; text-align: right; }
.qc-judges { display: flex; gap: 3px; justify-content: flex-end; }
.qc-dot {
  width: 18px; height: 18px; line-height: 18px; text-align: center; font-size: 10px;
  background: #e2e8f0; border-radius: 4px; color: #475569; font-style: normal;
}
.qc-dot.zero { color: #cbd5e1; background: #f1f5f9; }
.qc-risk { margin-top: 6px; font-size: 11px; color: #dc2626; font-weight: 600; }

/* ---- 评价 ---- */
.rate-footer { margin-top: 8px; border-top: 1px dashed var(--border); padding-top: 6px; }
.rate-btn {
  font-size: 12px; cursor: pointer; border: 1px solid var(--brand); color: var(--brand);
  background: #fff; border-radius: 999px; padding: 2px 12px; font-weight: 600;
}
.rate-btn:hover { background: color-mix(in srgb, var(--brand) 8%, #fff); }
.rated { display: flex; align-items: center; gap: 8px; }
.rated-label { font-size: 12px; color: var(--muted); }
.rate-edit {
  font-size: 11px; cursor: pointer; border: 1px solid var(--border); background: #fff;
  border-radius: 6px; padding: 1px 8px; color: var(--brand); margin-left: auto;
}
.rate-panel {
  margin-top: 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 10px;
}
.rate-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.rate-row-label { width: 56px; font-size: 12px; color: #475569; flex-shrink: 0; }
.stars { display: inline-flex; gap: 1px; }
.stars .star {
  font-size: 14px; color: #d1d5db; cursor: pointer; transition: color .12s, transform .12s;
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
  background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 4px 8px; margin-bottom: 6px;
}
.rate-comment {
  width: 100%; box-sizing: border-box; font-size: 12px; border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 6px; resize: vertical; font-family: inherit;
}
.rate-actions { display: flex; gap: 8px; margin-top: 6px; }
.rate-submit {
  font-size: 12px; cursor: pointer; border: none; background: var(--brand, #4f46e5); color: #fff;
  border-radius: 6px; padding: 4px 14px; font-weight: 600;
}
.rate-submit:disabled { opacity: 0.5; cursor: not-allowed; }
.rate-cancel {
  font-size: 12px; cursor: pointer; border: 1px solid var(--border); background: #fff;
  border-radius: 6px; padding: 4px 12px; color: var(--muted);
}
</style>
