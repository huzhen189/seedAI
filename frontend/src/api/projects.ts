import type { Artifact, Conversation, Project, SearchItem, Message } from '../types'
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
/** 获取会话的消息列表(独立端点: /api/conversations/{id}/messages) */
export const getConversationMessages = (id: number): Promise<Message[]> =>
  get(`/api/conversations/${id}/messages`)
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

/** 拉取已生成站点的源码文件列表(可选含内容), 供「代码」视图展示。 */
export interface ArtifactFile {
  name: string
  size: number
  content?: string
}
export const listArtifactFiles = (
  projectId: number,
  artifactId: number,
  withContent = false,
): Promise<{ files: ArtifactFile[] }> =>
  get(
    `/api/projects/${projectId}/artifacts/${artifactId}/files${withContent ? '?with_content=1' : ''}`,
  )

/**
 * 拉取「当前(HEAD)版本」的生成物文件清单, 供发布弹窗勾选。
 * 通过后端 list_artifacts 取 head artifact, 再 list_artifact_files 列文件。
 */
export async function fetchPublishFileList(
  projectId: number,
  artifacts: { id: number; is_head?: boolean }[],
): Promise<ArtifactFile[]> {
  const head = artifacts.find((a) => a.is_head) ?? artifacts[artifacts.length - 1]
  if (!head) return []
  const res = await listArtifactFiles(projectId, head.id, false)
  // 发布清单自动排除文档(.md/.txt), 其余(HTML/CSS/JS/图片等)都可选。
  return res.files.filter((f) => !/\.(md|txt)$/i.test(f.name))
}
