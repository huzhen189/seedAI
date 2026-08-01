/**
 * 前端离线队列(M9c / REQ-OFFLINE-001)。
 *
 * 目标: 网络中断时, 用户发出的消息不丢失 —— 持久化到 IndexedDB, 联网后按
 * client_msg_id 串行、幂等补发(后端对同一 client_msg_id 重复提交只重挂接已有流,
 * 不会重复执行, 见 backend/app/api/turns.py 的幂等说明)。
 *
 * 状态机: pending → queued → sending → done | failed
 *   - pending: 离线时入队(或在线但发送前)
 *   - sending: 正在补发
 *   - done:   补发成功(随后从库清理)
 *   - failed: 补发失败(保留, 下次联网重试)
 */

export type OfflineStatus = 'pending' | 'queued' | 'sending' | 'done' | 'failed'

export interface QueuedMessage {
  client_msg_id: string
  message: string
  conversation_id?: number | null
  status: OfflineStatus
  created_at: number
  updated_at: number
  error?: string
}

const DB_NAME = 'seedai-offline'
const STORE = 'messages'
const DB_VERSION = 1

// 非浏览器环境(node / 测试)降级为内存存储, 保证模块可加载且不抛错。
const memory = new Map<string, QueuedMessage>()
const hasIDB = typeof indexedDB !== 'undefined'

let dbPromise: Promise<IDBDatabase> | null = null

function openDB(): Promise<IDBDatabase> {
  if (!hasIDB) return Promise.reject(new Error('IndexedDB 不可用'))
  if (dbPromise) return dbPromise
  dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'client_msg_id' })
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  return dbPromise
}

function runTx<T>(
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => IDBRequest,
): Promise<T> {
  return openDB().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const tx = db.transaction(STORE, mode)
        const req = fn(tx.objectStore(STORE))
        req.onsuccess = () => resolve(req.result as T)
        req.onerror = () => reject(req.error)
      }),
  )
}

function now(): number {
  return Date.now()
}

export const offlineQueue = {
  /** 入队一条离线消息(状态 pending)。 */
  async enqueue(msg: {
    client_msg_id: string
    message: string
    conversation_id?: number | null
  }): Promise<QueuedMessage> {
    const item: QueuedMessage = {
      client_msg_id: msg.client_msg_id,
      message: msg.message,
      conversation_id: msg.conversation_id ?? null,
      status: 'pending',
      created_at: now(),
      updated_at: now(),
    }
    if (hasIDB) {
      await runTx('readwrite', (store) => store.put(item))
    } else {
      memory.set(item.client_msg_id, item)
    }
    return item
  },

  /** 列出待补发的消息(pending / failed), 按创建时间升序。 */
  async pending(): Promise<QueuedMessage[]> {
    let all: QueuedMessage[]
    if (hasIDB) {
      all = (await runTx<QueuedMessage[]>('readonly', (store) => store.getAll())) as QueuedMessage[]
    } else {
      all = [...memory.values()]
    }
    return all
      .filter((i) => i.status === 'pending' || i.status === 'failed')
      .sort((a, b) => a.created_at - b.created_at)
  },

  async setStatus(
    clientMsgId: string,
    status: OfflineStatus,
    error?: string,
  ): Promise<void> {
    if (hasIDB) {
      const existing = await runTx<QueuedMessage | undefined>(
        'readonly',
        (store) => store.get(clientMsgId),
      )
      if (!existing) return
      const next: QueuedMessage = {
        ...existing,
        status,
        updated_at: now(),
        error: error ?? (status === 'failed' ? existing.error : undefined),
      }
      await runTx('readwrite', (store) => store.put(next))
    } else {
      const existing = memory.get(clientMsgId)
      if (!existing) return
      memory.set(clientMsgId, {
        ...existing,
        status,
        updated_at: now(),
        error: error ?? (status === 'failed' ? existing.error : undefined),
      })
    }
  },

  async remove(clientMsgId: string): Promise<void> {
    if (hasIDB) {
      await runTx('readwrite', (store) => store.delete(clientMsgId))
    } else {
      memory.delete(clientMsgId)
    }
  },

  async count(): Promise<number> {
    return (await this.pending()).length
  },

  /**
   * 串行补发: 逐条调用 processOne(幂等, 后端按 client_msg_id 去重)。
   * 任一条失败则停止本轮(保留后续待发, 下次联网继续), 保证顺序。
   * 成功后清理 done 条目。
   */
  async flush(processOne: (item: QueuedMessage) => Promise<void>): Promise<number> {
    const items = await this.pending()
    let sent = 0
    for (const item of items) {
      await this.setStatus(item.client_msg_id, 'sending')
      try {
        await processOne(item)
        await this.setStatus(item.client_msg_id, 'done')
        await this.remove(item.client_msg_id)
        sent += 1
      } catch (e) {
        await this.setStatus(
          item.client_msg_id,
          'failed',
          e instanceof Error ? e.message : String(e),
        )
        break // 顺序保证: 前一条失败则后续留待下次
      }
    }
    return sent
  },
}

export function isOnline(): boolean {
  return typeof navigator === 'undefined' ? true : navigator.onLine
}

/** 注册联网回调(仅触发一次), 返回注销函数。 */
export function onOnline(cb: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  const handler = () => cb()
  window.addEventListener('online', handler)
  return () => window.removeEventListener('online', handler)
}
