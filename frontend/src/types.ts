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

/** 澄清卡结构化选项(前端浮动卡片渲染). */
export interface ClarifyOption {
  /** 选项展示文案 */
  label: string
  /** 是否为系统推荐选项(前端标"推荐") */
  recommended?: boolean
}

/** 澄清(CLARIFY): 意图模糊/缺规格时下发, 前端以底部浮动卡片呈现. */
export interface ClarifyEvent {
  /** 自然语言追问(兜底展示) */
  questions?: string[]
  /** 已进行的澄清轮次 */
  rounds?: number
  /** 结构化候选选项(单选/多选) */
  options?: ClarifyOption[]
  /** 是否多选(默认 false 单选) */
  multi?: boolean
  /** 自由输入框提示语 */
  freeTextHint?: string
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

/** Phase 1(WorkBuddy 式 think→call→observe):工具调用 / 结果 / 思考的前端视图模型。
 *  与 backend/app/agent/tools/__init__.py 的 emit_* 形状对齐。 */
export type ToolTrailEntry =
  | { kind: 'reasoning'; text: string; sub_task_id?: string }
  | {
      kind: 'tool'
      call_id: string
      name: string
      /** 工具入参(已截断展示用) */
      args: Record<string, any>
      status: 'pending' | 'done'
      ok?: boolean
      /** 结果摘要(默认折叠) */
      summary?: string
      sub_task_id?: string
    }

/** reasoning 事件(非流式思考/计划/意图)。 */
export interface ReasoningEvent {
  text?: string
  sub_task_id?: string
}
/** tool_call 事件(工具调用开始)。 */
export interface ToolCallEvent {
  tool_call_id?: string
  name?: string
  args?: Record<string, any> | null
  sub_task_id?: string
}
/** tool_result 事件(工具调用结果)。 */
export interface ToolResultEvent {
  tool_call_id?: string
  name?: string
  ok?: boolean
  summary?: string
  sub_task_id?: string
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
  /** 当前 active 部署的线上地址(已发布才非空; 否则 null)。 */
  published_url?: string | null
  head_artifact_id?: number | null
  published_artifact_id?: number | null
  active_deployment_id?: number | null
  has_unpublished_changes?: boolean
  requirement_doc?: string | null  // 需求文档(JSON 字符串), 重启后还原右侧"📋 需求文档"
}

export interface Message {
  id: number
  conversation_id: number
  role: string
  content: string
  model_id: string | null
  trace_id?: string | null
  /** 本轮 turn_id，用于「修改上一条」回溯控制(correct/supplement)定位 prior turn */
  turn_id?: string | null
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
  summary?: string  // AI 生成后的文字总结(v1.2.2), 刷新后仍可展示
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

/** 非阻塞候选项提示: 系统已自行决定 top-1, 同时给出可切换的候选(用户可说"用 X"/"B"切换)。 */
export interface AlternativesEvent {
  /** 系统已选的 skill 名 */
  selected: string
  /** 可切换的候选 skill 名列表 */
  skills: string[]
  /** 提示文案 */
  hint?: string
}

export interface Conversation {
  id: number
  project_id: number
  user_id: number
  name: string | null
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
  account: string
  nickname: string
  email: string | null
  role: string
  plan: string
}

/** 管理后台实时指标快照(/admin/metrics SSE)。 */
export interface ModelUsageItem {
  tokens: number
  count: number
  est_cost: number
  raw_count: number
}

export interface LatencyBucket {
  p50: number
  p90: number
  p99: number
  avg: number
  samples: number
}

export interface MetricsSnapshot {
  uptime_s?: number
  requests_total?: number
  requests_error?: number
  requests_per_min?: number
  model_usage?: Record<string, ModelUsageItem>
  /** API 接口延迟(R1): 业务端 + 需求端(AI 核心 7102)两组, 各为 path -> 百分位统计 */
  api_latency?: {
    business: Record<string, LatencyBucket>
    ai_service: Record<string, LatencyBucket>
  }
  ai_stats?: { generate_total: number; v090_features: Record<string, number> }
  /** 三库健康状态: MySQL + Redis + Chroma(向量库), 含容量/连接信息(R2) */
  db?: DbStatus
  /** 运行主机(操作系统)指标: 名称 / 内存 / CPU / 磁盘 / 开机时长 */
  system?: SystemStatus
  error?: string
}

/** 运行主机(操作系统)指标(后端 _system_status)。 */
export interface SystemStatus {
  platform: { name: string; family: string }   // 友好名称 + linux/windows/darwin
  hostname?: string
  kernel?: string                              // 内核版本(platform.release)
  arch?: string                                // x86_64 / aarch64 等
  python_version?: string
  cpu_cores?: number | null
  cpu_percent?: number | null                  // 0~100
  load_avg?: number[] | null                   // [1m,5m,15m]
  boot_time?: number | null                    // epoch 秒(算开机时长)
  mem?: {
    ok: boolean
    total?: number
    available?: number
    used?: number
    percent?: number | null
    error?: string
  }
  disk?: {
    ok: boolean
    total?: number
    used?: number
    free?: number
    percent?: number | null
    partitions?: {
      device: string
      mountpoint: string
      fstype: string
      total: number
      used: number
      free: number
      percent?: number | null
    }[]
    error?: string
  }
  ts?: number
}

/** 单库容量展示(R2): 具体数值 + 百分比两行 */
export interface DbCapacity {
  value: string
  value_bytes?: number
  pct?: number | null
  detail?: string
}

export interface MySqlStatus {
  ok: boolean
  pool_size?: number
  checked_in?: number
  overflow?: number
  capacity?: DbCapacity
  max_connections?: number
  threads_connected?: number
  error?: string
}

export interface RedisStatus {
  ok: boolean
  capacity?: DbCapacity
  used_memory_human?: string
  maxmemory_human?: string
  connected_clients?: number
  db_keys?: number
  error?: string
}

export interface ChromaStatus {
  ok: boolean
  capacity?: DbCapacity
  collection_count?: number
  item_count?: number
  collections?: { name: string; count: number }[]
  error?: string
}

export interface DbStatus {
  mysql?: MySqlStatus
  redis?: RedisStatus
  chroma?: ChromaStatus
}

/** 生成产物(Artifact 表)
 *  v3 形状(`GET /api/projects/{id}/artifacts` 返回): 不再内联 files 列表,
 *  只暴露版本级元数据; 实际产物经「签名预览端点」(`/preview/{token}/...`)按短期签名提供。
 */
export interface Artifact {
  id: number
  project_id?: number
  version?: number
  trace_id?: string
  repo?: string        // site | code | image | doc
  status?: string      // building | verified | preview_ready | failed | deleted
  /** 是否可签发预览(verified/preview_ready 且有 preview_path)。 */
  previewable?: boolean
  is_head?: boolean
  is_published?: boolean
  capability_manifest?: Record<string, unknown> | null
  manifest_digest?: string | null
  created_at?: string
}

/** 管理后台用户列表项(/admin/users)。 */
export interface AdminUser {
  id: number
  account: string
  display_name: string
  email: string | null
  role: string
  tier: string
}

// ---------- 后置 QC(单裁判 v2.3.0) ----------
// 维度顺序须与 backend/app/agent/qc.py 保持一致(雷达图轴序)。
// v2.3.0 起不再固定三裁判: scores / judges 长度 = 实际参与评分的模型数(通常 1)。
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

// v2.3.0 单裁判: 实际 QC 模型不再固定, 由后端运行时决定(model=本次生成所用模型)。
// 保留 QC_JUDGES 仅作历史兼容占位; 雷达图序列名以后端返回的 qc_judges(实际出现模型)为准。
export const QC_JUDGES: string[] = []

/** 单裁判结果(整体评分用不到原始分, 明细走 dimensions.scores) */
export interface QcJudgeResult {
  model: string
  valid: boolean
  comment: string
}

/** 单维度聚合: 均值 + 方差 + 各裁判原始分(长度=实际裁判数, 0=未参与/失败) */
export interface QcDimensionScore {
  mean: number
  variance: number
  scores: number[] // 长度=实际裁判数; 单裁判时长度=1
}

/** QC 聚合结果(即 SSE `qc` 事件 data, 亦为 qc_scores.result) */
export interface QcResult {
  judges: QcJudgeResult[]
  dimensions: Record<QcDimension, QcDimensionScore>
  overall: number
  needs_review: boolean
  safety_risk: string // low|medium|high|critical
  partial: boolean
  /** per-sub-task QC 时带子任务 id(整体/编排 QC 为 undefined)。前端据此区分「整体分」与「子任务分」。 */
  sub_task_id?: string
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
  | 'cancelled'   // §6 A/B: 用户取消(进行中/已调度被中止)
  | 'aborted'     // §6 A: 连接断开导致的硬中止
  | 'paused'      // §6 A: 暂停(await_confirm 等断点), 可恢复

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

/** §6 D: 取消结构化摘要(编排层级联取消后下发, 供前端渲染「取消摘要」卡)。 */
export interface CancelSummaryEvent {
  /** 被用户取消的子任务 id 列表(进行中/已调度被中止) */
  cancelled: string[]
  /** 已正常完成的子任务 id 列表 */
  completed: string[]
  /** 未开始被级联跳过的子任务 id 列表 */
  skipped: string[]
}

/** §9: 执行前计划预览(意图识别后、执行前下发, 所有意图通用)。 */
export interface PlanPreviewEvent {
  /** 计划标题(如「执行计划」) */
  title?: string
  /** 补充说明文案 */
  note?: string
  /** SOP 角色链路(按顺序): product / design / dev / qa 的子集或全部 */
  roles?: string[]
}

/** §9: SOP 四角色链路(产品分析师 → 设计顾问 → 开发工程师 → 质量评审)。 */
export const SOP_ROLES: { key: string; label: string }[] = [
  { key: 'product', label: '产品分析师' },
  { key: 'design', label: '设计顾问' },
  { key: 'dev', label: '开发工程师' },
  { key: 'qa', label: '质量评审' },
]

/** skill → SOP 角色(与后端 handoff.ROLE_FOR_SKILL 对齐, 前端用于高亮当前阶段)。 */
export const SKILL_TO_ROLE: Record<string, string> = {
  agent_requirement: 'product',
  agent_design: 'design',
  agent_build: 'dev',
  agent_generate_site: 'dev',
  agent_doc: 'dev',
  agent_review: 'qa',
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
  /** 该子任务自身的后置 QC 单裁判结果(per-sub-task 打分, v2.3.x) */
  qc?: QcResult
}
