import type { Artifact, Conversation, Project, SearchItem } from '../types'
import { del, get, patch, post } from './client'

export const listProjects = (): Promise<Project[]> => get('/api/projects')
export const createProject = (name: string): Promise<Project> =>
  post('/api/projects', { name })
export const renameProject = (id: number, name: string): Promise<Project> =>
  patch(`/api/projects/${id}`, { name })
export const deleteProject = (id: number): Promise<null> => del(`/api/projects/${id}`)

export const listConversations = (projectId: number): Promise<Conversation[]> =>
  get(`/api/conversations?project_id=${projectId}`)
export const createConversation = (projectId: number, title?: string): Promise<Conversation> =>
  post('/api/conversations', { project_id: projectId, title })
export const getConversation = (id: number): Promise<Conversation> =>
  get(`/api/conversations/${id}`)
export const renameConversation = (id: number, name: string): Promise<Conversation> =>
  patch(`/api/conversations/${id}`, { name })
export const deleteConversation = (id: number): Promise<null> =>
  del(`/api/conversations/${id}`)

export const search = (q: string): Promise<SearchItem[]> =>
  get(`/api/search?q=${encodeURIComponent(q)}`)

export const listArtifacts = (projectId: number): Promise<Artifact[]> =>
  get(`/api/projects/${projectId}/artifacts`)
