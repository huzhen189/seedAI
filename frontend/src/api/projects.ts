import type { Artifact, Conversation, Project, SearchItem } from '../types'
import { del, get, patch, post } from './client'

export { patch }

export const listProjects = (): Promise<Project[]> => get('/api/projects')
export const createProject = (name: string): Promise<Project> =>
  post('/api/projects', { name })
export const renameProject = (id: number, name: string): Promise<Project> =>
  patch(`/api/projects/${id}`, { name })
export const deleteProject = (id: number): Promise<null> => del(`/api/projects/${id}`)

export const listConversations = (projectId: number): Promise<Conversation[]> =>
  get(`/api/conversations?project_id=${projectId}`)
export const createConversation = (projectId: number, name?: string): Promise<Conversation> =>
  post('/api/conversations', { project_id: projectId, name })
export const getConversation = (id: number): Promise<Conversation> =>
  get(`/api/conversations/${id}`)
export const renameConversation = (id: number, name: string): Promise<Conversation> =>
  patch(`/api/conversations/${id}`, { name })
export const deleteConversation = (id: number): Promise<null> =>
  del(`/api/conversations/${id}`)

/** 首条对话无项目时: 按对话文本自动建项目+会话, 返回 {project, conversation}。 */
export const autoStart = (
  text: string,
): Promise<{ project: Project; conversation: Conversation }> =>
  post('/api/auto-start', { text })

export const search = (q: string): Promise<SearchItem[]> =>
  get(`/api/search?q=${encodeURIComponent(q)}`)

export interface MessageSearchResult {
  message_id: number
  conversation_id: number
  project_id: number
  project_name: string
  conv_title: string
  user_text: string
  ai_reply: string
  created_at: string
}

export const searchMessages = (q: string): Promise<MessageSearchResult[]> =>
  get(`/api/search/messages?q=${encodeURIComponent(q)}`)

export const listArtifacts = (projectId: number): Promise<Artifact[]> =>
  get(`/api/projects/${projectId}/artifacts`)
