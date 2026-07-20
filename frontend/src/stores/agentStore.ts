/** Agent 注册表(从 /api/agents 加载, 运行时缓存)。
 * SSE 只传 agent_id, 前端据此展示名称/头像。
 */
export interface AgentMeta {
  id: string
  name: string
  avatar: string
  role: string
  description: string
}

let _registry: Map<string, AgentMeta> | null = null
let _loading: Promise<void> | null = null

export async function loadAgents(): Promise<Map<string, AgentMeta>> {
  if (_registry) return _registry
  if (_loading) return _loading.then(() => _registry!)
  _loading = (async () => {
    const resp = await fetch('/api/agents')
    if (!resp.ok) throw new Error(`load agents failed: ${resp.status}`)
    const data: AgentMeta[] = await resp.json()
    _registry = new Map()
    for (const a of data) {
      _registry.set(a.id, a)
      // 别名: build_agent_coder → build_agent
      if (a.id === 'build_agent') _registry.set('build_agent_coder', a)
    }
  })()
  await _loading
  return _registry!
}

export function getAgent(agentId: string): AgentMeta | undefined {
  return _registry?.get(agentId)
}

/** 从 SSE data 中提取 agent_id 并查找 meta */
export function resolveAgent(data: Record<string, unknown>): { id: string; meta?: AgentMeta } {
  const id = (data.agent_id as string) || (data.skill as string) || ''
  return { id, meta: getAgent(id) }
}
