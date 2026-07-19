import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as projectsApi from '../api/projects'
import type { Conversation, Message } from '../types'

export const useConversationStore = defineStore('conversation', () => {
  const conversations = ref<Conversation[]>([])       // 当前项目所有会话(倒序)
  const currentConvId = ref<number | null>(null)       // 当前活跃会话(最新一条)
  const messages = ref<Message[]>([])                  // 合并多轮会话消息(时间轴)
  const loading = ref(false)
  const loadingMore = ref(false)                       // 上翻加载更早会话中
  const loadedConvIdx = ref(0)                         // 已加载到第几个会话(从 0 开始)
  const pendingConvId = ref<number | null>(null)

  /** 切换项目时加载会话列表并取最新会话的消息。 */
  async function loadConversations(projectId: number) {
    loading.value = true
    try {
      conversations.value = await projectsApi.listConversations(projectId)
      loadedConvIdx.value = 0
      messages.value = []
      if (conversations.value.length) {
        await loadMessagesInto(conversations.value[0].id, 'replace')
      }
    } finally {
      loading.value = false
    }
  }

  /** 上翻加载更早的会话消息(追加到 messages 头部)。 */
  async function loadMoreHistory(): Promise<boolean> {
    const nextIdx = loadedConvIdx.value + 1
    if (nextIdx >= conversations.value.length) return false
    loadingMore.value = true
    try {
      await loadMessagesInto(conversations.value[nextIdx].id, 'prepend')
      loadedConvIdx.value = nextIdx
      return true
    } finally {
      loadingMore.value = false
    }
  }

  /** 加载指定的会话消息, replace=替换现有 / prepend=头部追加。 */
  async function loadMessagesInto(convId: number, mode: 'replace' | 'prepend') {
    const c = await projectsApi.getConversation(convId)
    const newMsgs = c.messages || []
    if (mode === 'replace') {
      messages.value = newMsgs
      currentConvId.value = convId
    } else {
      messages.value = [...newMsgs, ...messages.value]
    }
  }

  /** 新建会话(当前项目下), 清空消息区。 */
  async function create(projectId: number, title?: string): Promise<Conversation> {
    const c = await projectsApi.createConversation(projectId, title)
    conversations.value.unshift(c)
    currentConvId.value = c.id
    messages.value = []
    loadedConvIdx.value = 0
    return c
  }

  async function remove(id: number) {
    await projectsApi.deleteConversation(id)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentConvId.value === id) {
      currentConvId.value = conversations.value[0]?.id ?? null
      if (currentConvId.value) await loadMessagesInto(currentConvId.value, 'replace')
      else messages.value = []
    }
  }

  function reset() {
    conversations.value = []
    currentConvId.value = null
    messages.value = []
    loadedConvIdx.value = 0
  }

  return {
    conversations, currentConvId, messages, loading, loadingMore,
    loadedConvIdx, pendingConvId,
    loadConversations, loadMoreHistory, loadMessagesInto, create, remove, reset,
  }
})
