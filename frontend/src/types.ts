export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ModelInfo {
  id: string
  label: string
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
  created_at: string
}

export interface Conversation {
  id: number
  project_id: number
  user_id: number
  title: string | null
  created_at: string
  updated_at: string
  messages?: Message[]
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
  error?: string
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
