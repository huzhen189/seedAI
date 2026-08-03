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

/** 读取缓存, 不存在返回 null(附带去重自愈, 防御历史脏缓存) */
export function loadCache(projectId: number): MessageCacheEntry[] | null {
  try {
    const raw = localStorage.getItem(key(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as MessageCacheEntry[]
    return dedupeMessages(parsed)
  } catch {
    return null
  }
}

/** 写入缓存 */
export function saveCache(projectId: number, messages: MessageCacheEntry[]): void {
  try {
    // 关键: 绝不把流式乐观占位(message.id <= 0, 来自 optimisticMessage 的 id:0 助手气泡)
    // 沉淀进缓存。否则下次刷新时缓存里的 id:0 占位会和 DB 真实行(id>0)并存, 渲染成
    // "两条 content 一模一样"的重复消息。只缓存已落库的真实消息(id>0)。
    const persisted = messages.filter((m) => m.id > 0)
    localStorage.setItem(key(projectId), JSON.stringify(persisted))
  } catch {
    // localStorage 满则清空该项目的缓存(其他项目不受影响)
    localStorage.removeItem(key(projectId))
  }
}

/**
 * 加载缓存时自愈去重(防御历史脏缓存)。
 *
 * 双回复 bug 时代曾把 id:0 占位 + 真实 id>0 行一起写进了 localStorage, 导致刷新后
 * 出现两条 content 相同的消息。这里以「真实 id 行」为权威: 同 (role+content) 下若已
 * 有 id>0 消息, 直接丢弃 id<=0 的占位, 把历史脏数据自愈为单条。
 */
export function dedupeMessages(messages: MessageCacheEntry[]): MessageCacheEntry[] {
  const realByKey = new Map<string, MessageCacheEntry>()
  for (const m of messages) {
    if (m.id > 0) realByKey.set(`${m.role}|${m.content}`, m)
  }
  const out: MessageCacheEntry[] = []
  for (const m of messages) {
    if (m.id > 0) {
      if (!out.includes(m)) out.push(m)
    } else if (!realByKey.has(`${m.role}|${m.content}`)) {
      // 仅保留未被真实行覆盖的占位(兜底, 正常已被 saveCache 过滤)
      out.push(m)
    }
  }
  return out.sort((a, b) => a.id - b.id)
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
