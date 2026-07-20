/** localStorage 消息缓存工具。
 *
 * Key 格式: seedai_msg_{projectId}
 * Value: MessageCacheEntry[] (按 id 升序, 最近的在末尾)
 */
export interface MessageCacheEntry {
  id: number
  conversation_id: number
  role: 'user' | 'assistant'
  content: string
  trace_id?: string | null
  created_at?: string | null
}

const PREFIX = 'seedai_msg_'

function key(projectId: number): string {
  return `${PREFIX}${projectId}`
}

/** 读取缓存, 不存在返回 null */
export function loadCache(projectId: number): MessageCacheEntry[] | null {
  try {
    const raw = localStorage.getItem(key(projectId))
    if (!raw) return null
    return JSON.parse(raw) as MessageCacheEntry[]
  } catch {
    return null
  }
}

/** 写入缓存 */
export function saveCache(projectId: number, messages: MessageCacheEntry[]): void {
  try {
    localStorage.setItem(key(projectId), JSON.stringify(messages))
  } catch {
    // localStorage 满则清空该项目的缓存(其他项目不受影响)
    localStorage.removeItem(key(projectId))
  }
}

/** 追加消息到缓存末尾(对话完成后调用) */
export function appendToCache(projectId: number, msg: MessageCacheEntry): void {
  const existing = loadCache(projectId) || []
  // 去重
  if (!existing.some((m) => m.id === msg.id)) {
    existing.push(msg)
    existing.sort((a, b) => a.id - b.id)
    saveCache(projectId, existing)
  }
}

/** 获取缓存的最后一条消息 id(用于判断是否还有更多) */
export function getMinIdInCache(projectId: number): number | null {
  const cached = loadCache(projectId)
  if (!cached || cached.length === 0) return null
  return cached[0].id
}

/** 获取缓存的最近 N 条(用于首屏展示) */
export function getRecentFromCache(projectId: number, n = 10): MessageCacheEntry[] {
  const cached = loadCache(projectId)
  if (!cached) return []
  return cached.slice(-n)
}

/** 合并新数据到缓存(上拉加载更多时, 新数据追加到头部) */
export function mergeToCache(projectId: number, older: MessageCacheEntry[]): void {
  const existing = loadCache(projectId) || []
  const existIds = new Set(existing.map((m) => m.id))
  const merged = [...older.filter((m) => !existIds.has(m.id)), ...existing]
  merged.sort((a, b) => a.id - b.id)
  saveCache(projectId, merged)
}

/** 删除项目的所有消息缓存 */
export function clearCache(projectId: number): void {
  localStorage.removeItem(key(projectId))
}
