// @vitest-environment node
import { describe, it, expect, beforeEach } from 'vitest'
import { offlineQueue } from './offlineQueue'

describe('offlineQueue (M9c)', () => {
  beforeEach(async () => {
    // 清空遗留待发(内存/IndexedDB 降级路径均可)。
    const items = await offlineQueue.pending()
    for (const item of items) await offlineQueue.remove(item.client_msg_id)
  })

  it('enqueues messages and flushes them serially in creation order', async () => {
    const order: string[] = []
    await offlineQueue.enqueue({ client_msg_id: 'a', message: 'first', conversation_id: 1 })
    await offlineQueue.enqueue({ client_msg_id: 'b', message: 'second', conversation_id: 1 })

    expect(await offlineQueue.count()).toBe(2)

    const sent = await offlineQueue.flush(async (item) => {
      order.push(item.message)
    })

    expect(sent).toBe(2)
    expect(order).toEqual(['first', 'second'])
    expect(await offlineQueue.count()).toBe(0)
  })

  it('stops on first failure and keeps remaining items for retry', async () => {
    await offlineQueue.enqueue({ client_msg_id: 'a', message: 'first', conversation_id: 1 })
    await offlineQueue.enqueue({ client_msg_id: 'b', message: 'second', conversation_id: 1 })

    const sent = await offlineQueue.flush(async (item) => {
      if (item.message === 'first') throw new Error('boom')
    })

    expect(sent).toBe(0)
    // 失败项保留, 后续项也保留, 等待下次联网补发。
    expect(await offlineQueue.count()).toBe(2)
  })
})
