import type { Conversation, Project, SearchItem } from '../types'
import { notifyAuthRequired } from '../stores/auth'

async function j(method: string, path: string, body?: unknown): Promise<any> {
  const r = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include',
  })
  if (!r.ok) {
    const text = await r.text().catch(() => '')
    if (r.status === 401) {
      // 鉴权失效:主动弹出登录框,并给出明确提示
      notifyAuthRequired()
      throw new Error('登录已失效，请重新登录')
    }
    throw new Error(text || `HTTP ${r.status}`)
  }
  if (r.status === 204) return null
  return r.json()
}

export const listProjects = (): Promise<Project[]> => j('GET', '/api/projects')
export const createProject = (name: string): Promise<Project> =>
  j('POST', '/api/projects', { name })
export const renameProject = (id: number, name: string): Promise<Project> =>
  j('PATCH', `/api/projects/${id}`, { name })
export const deleteProject = (id: number): Promise<null> => j('DELETE', `/api/projects/${id}`)

export const listConversations = (projectId: number): Promise<Conversation[]> =>
  j('GET', `/api/conversations?project_id=${projectId}`)
export const createConversation = (
  projectId: number,
  title?: string,
): Promise<Conversation> => j('POST', '/api/conversations', { project_id: projectId, title })
export const getConversation = (id: number): Promise<Conversation> =>
  j('GET', `/api/conversations/${id}`)
export const renameConversation = (id: number, name: string): Promise<Conversation> =>
  j('PATCH', `/api/conversations/${id}`, { name })
export const deleteConversation = (id: number): Promise<null> =>
  j('DELETE', `/api/conversations/${id}`)

export const search = (q: string): Promise<SearchItem[]> =>
  j('GET', `/api/search?q=${encodeURIComponent(q)}`)
