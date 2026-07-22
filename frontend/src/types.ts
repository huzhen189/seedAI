export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ModelInfo {
  id: string
  label: string
  version?: string
  speed?: string
  desc?: string
}

export interface NodeEvent {
  stage?: string
  skill?: string
  url?: string | null
  fallback?: string | null
  attempt?: number
  retry?: boolean
  [k: string]: unknown
}

export interface ThinkEvent {
  stage?: string
  content?: string
  passed?: boolean
  comment?: string
}

/** 大计划 / 目标特殊节点(Planner 产出,前端渲染为「计划 / 流程」卡片)。 */
export interface PlanEvent {
  title?: string
  goal?: string
  steps?: string[]
}

/** 主模型不可用,携带可选替代列表,前端弹框确认后重发 */
export interface RetryEvent {
  failed?: string
  suggested?: string[]
  message?: string
}

/** 意图识别结果(前端显示标签 + 控制面板). */
export interface IntentEvent {
  level1?: string
  level2?: string
  label?: string
  level1_label?: string
  level2_label?: string
  confidence?: number
  industry?: string
  /** 汇总器决策: route|block|confirm|options|fallback */
  decision?: string
  /** 安全等级: low|medium|high|critical */
  risk_level?: string
  requires_confirm?: boolean
  /** 汇总器算定的最终技能(单一来源) */
  selected_skill?: string
  /** 计划(含 options 候选 / confirm 原因 / block 原因) */
  plan?: { action: string; reason?: string; skill?: string; skills?: string[] }[]
}

/** 不支持的功能提示. */
export interface UnsupportedEvent {
  message?: string
}

/** 高危拦截(安全 critical, 不可绕过). */
export interface BlockEvent {
  reason?: string
}

/** 二次确认(安全 high, 等待用户确认后带 confirmed 重发). */
export interface ConfirmEvent {
  reason?: string
  /** 确认后要执行的技能名 */
  skill?: string
}

/** 思考时间线中的一步:每个 agent 节点对应一步,含该步的思考文本与状态。 */
export interface ThoughtStep {
  stage: string
  label: string
  status: 'pending' | 'active' | 'done'
  think: string
  passed?: boolean
  comment?: string
}

/** 用户角色(RBAC 三级)。 */
export type Role = 'user' | 'admin' | 'super_admin'
export const ROLE_LABELS: Record<Role, string> = {
  user: '普通用户',
  admin: '管理员',
  super_admin: '超级管理员',
}

export interface PreviewEvent {
  url?: string
  stage?: string
}

// ---------- 项目 / 会话 / 消息 (M1) ----------
export interface Project {
  id: number
  user_id: number
  name: string
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  conversation_id: number
  role: string
  content: string
  model_id: string | null
  trace_id?: string | null
  created_at: string
}

// ---- 消息 content 解析后的类型（content 为 JSON 字符串时） ----
export type ContentData = PlainContent | SiteContent | CodeContent | ImageContent | ErrorContent | TrailContent

export interface PlainContent {
  type: 'plain'
  text: string
}

export interface SiteContent {
  type: 'site'
  artifact_id: number
  title: string
  preview_url: string
  download_url?: string
  files: { name: string; size: number }[]
}

export interface CodeContent {
  type: 'code'
  artifact_id: number
  title: string
  language: string
  code_preview?: string
}

export interface ImageContent {
  type: 'image'
  url: string
  title?: string
}

export interface ErrorContent {
  type: 'error'
  message: string
}

export interface TrailContent {
  type: 'trail'
  events: { event: string; data: Record<string, any> }[]
}

/** 多方案选择(options 事件,前端弹出单选框) */
export interface OptionEvent {
  question?: string
  choices?: { id: string; title: string; desc?: string; pros?: string; cons?: string }[]
  /** "skill": 管道级多选项, 选中即带 skill 参数重发; 省略: requirement_agent 文本拼接模式 */
  mode?: string
}

export interface Conversation {
  id: number
  project_id: number
  user_id: number
  title: string | null
  created_at: string
  updated_at: string
  messages?: Message[]
  status?: string  // active | paused | completed | aborted | error
  checkpoint_stage?: string | null  // planner_done | coder_done | reviewer_r1..
  checkpoint_data?: string | null   // JSON
  progress_pct?: number  // 0~100
}

export interface SearchItem {
  type: string // project | conversation
  id: number
  title: string
  project_id: number | null
}

export interface AuthUser {
  id: number
  username: string
  nickname: string
  email: string | null
  role: string
  plan: string
}

/** 管理后台实时指标快照(/admin/metrics SSE)。 */
export interface MetricsSnapshot {
  uptime_s?: number
  requests_total?: number
  requests_error?: number
  requests_per_min?: number
  model_usage?: Record<string, number>
  /** 三库健康状态(业务可检的 MySQL + Redis) */
  db?: DbStatus
  error?: string
}

export interface DbStatus {
  mysql?: { ok: boolean; pool_size?: number; checked_in?: number; overflow?: number; error?: string }
  redis?: { ok: boolean; error?: string }
}

/** 生成产物(Artifact 表) */
export interface Artifact {
  id: number
  title?: string
  trace_id?: string
  repo?: string        // site | code | image | doc
  files?: Record<string, { name: string; size: number; url?: string }>
  preview_url?: string
  download_url?: string
  status?: string      // uploading | done | failed
  created_at?: string
}

/** 管理后台用户列表项(/admin/users)。 */
export interface AdminUser {
  id: number
  username: string
  nickname: string
  email: string | null
  role: string
  plan: string
}

// ---------- 后置 QC 三裁判(v0.8.5 M1) ----------
// 维度与 3 裁判顺序须与 backend/ai_service/app/qc.py 保持一致(雷达图轴序 + scores 下标)
export const QC_DIMENSIONS = [
  'correctness',
  'completeness',
  'compliance',
  'efficiency',
  'readability',
  'safety',
] as const
export type QcDimension = (typeof QC_DIMENSIONS)[number]

export const QC_DIM_LABELS: Record<QcDimension, string> = {
  correctness: '正确性',
  completeness: '完整性',
  compliance: '合规性',
  efficiency: '效率',
  readability: '可读性',
  safety: '安全性',
}

export const QC_JUDGES = ['deepseek', 'qwen', 'hy3'] as const
export type QcJudge = (typeof QC_JUDGES)[number]

/** 单裁判结果(整体评分用不到原始分, 明细走 dimensions.scores) */
export interface QcJudgeResult {
  model: QcJudge
  valid: boolean
  comment: string
}

/** 单维度聚合: 均值 + 方差 + 三裁判原始分(对齐 QC_JUDGES) */
export interface QcDimensionScore {
  mean: number
  variance: number
  scores: number[] // [deepseek, qwen, hy3], 0=未参与/失败
}

/** QC 三裁判聚合结果(即 SSE `qc` 事件 data, 亦为 qc_scores.result) */
export interface QcResult {
  judges: QcJudgeResult[]
  dimensions: Record<QcDimension, QcDimensionScore>
  overall: number
  needs_review: boolean
  safety_risk: string // low|medium|high|critical
  partial: boolean
}

/** 用户气泡内多维度评价(6 维各 1-10) */
export type RatingDims = Partial<Record<QcDimension, number>>

// ---------- 多意图编排(§多意图 v1.0) ----------
// 事件形状须与 backend/ai_service/app/core/orchestrator.py 的 ev(...) 调用保持一致。

/** 风险等级(与 safety.py / models.py 对齐) */
export type RiskLevel = 'high' | 'medium' | 'low'

/** 子任务状态(与 models.py SUB_* 常量对齐) */
export type SubTaskStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'blocked'
  | 'skipped'

/** 编排总览中的单个子任务元信息 */
export interface SubTaskMeta {
  id: string
  goal: string
  skill: string
  risk: RiskLevel
  status: SubTaskStatus
  dependencies: string[]
}

/** 编排总览事件(orchestration):子任务清单 + 执行策略 */
export interface OrchestrationEvent {
  total: number
  /** parallel = 全并行; mixed = 分层串行 + 层内并行 */
  strategy: 'parallel' | 'mixed'
  tasks: SubTaskMeta[]
}

/** 子任务开始进入执行层(subtask_start) */
export interface SubTaskStartEvent {
  sub_task_id: string
  goal: string
  skill: string
  risk: RiskLevel
  layer: number
}

/** 子任务完成(subtask_done) */
export interface SubTaskDoneEvent {
  sub_task_id: string
  /** 产出摘要(截断 200 字) */
  result_summary: string
  /** 产物 URL 列表(如站点预览) */
  artifacts: string[]
}

/** 子任务失败 / 拦截 / 跳过(subtask_fail) */
export interface SubTaskFailEvent {
  sub_task_id: string
  reason: string
  /** false = 高风险拦截不可恢复; true = 可重试 / 待确认 */
  recoverable: boolean
}

/** 合并结果中的失败子任务条目 */
export interface FailedSubTask {
  id: string
  goal: string
  error: string
}

/** 结果合并完成(merge):最终连贯回复 + 部分失败清单 */
export interface MergeEvent {
  success_count: number
  fail_count: number
  failed_tasks: FailedSubTask[]
  /** 合并后的完整中文回复(亦会作为 sub_task_id="__merge__" 的 token 流发送) */
  text: string
}

/** 前端运行时子任务视图模型(由 orchestration 初始化, 经事件增量更新)。 */
export interface SubTaskView extends SubTaskMeta {
  /** 执行层(仅 mixed 策略有意义) */
  layer?: number
  /** 该子任务自身产出的流式 token 累积 */
  tokens: string
  /** 完成后的产出摘要 */
  result_summary?: string
  /** 产物 URL 列表 */
  artifacts: string[]
  /** 失败 / 拦截 / 跳过原因 */
  fail_reason?: string
  /** 是否可恢复(false=高风险拦截; true=可重试/待确认) */
  recoverable?: boolean
}
