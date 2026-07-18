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

export interface PreviewEvent {
  url?: string
  stage?: string
}
