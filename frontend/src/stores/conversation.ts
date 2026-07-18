import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as projectsApi from '../api/projects'
import type { Conversation, Message } from '../types'

export const useConversationStore = defineStore('conversation', () => {
  const conversations = ref<Conversation[]>([])
  const currentConvId = ref<number | null>(null)
  const messages = ref<Message[]>([]) // 当前会话历史(加载后),发送时本地追加
  const loading = ref(false)
  const pendingConvId = ref<number | null>(null) // 搜索跳转待选中会话

  async function loadConversations(projectId: number) {
    conversations.value = await projectsApi.listConversations(projectId)
  }

  async function loadMessages(convId: number) {
    const c = await projectsApi.getConversation(convId)
    messages.value = c.messages || []
    currentConvId.value = convId
  }

  async function create(projectId: number, title?: string): Promise<Conversation> {
    const c = await projectsApi.createConversation(projectId, title)
    conversations.value.unshift(c)
    currentConvId.value = c.id
    messages.value = []
    return c
  }

  async function remove(id: number) {
    await projectsApi.deleteConversation(id)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (currentConvId.value === id) {
      currentConvId.value = null
      messages.value = []
    }
  }

  function reset() {
    conversations.value = []
    currentConvId.value = null
    messages.value = []
  }

  return {
    conversations,
    currentConvId,
    messages,
    loading,
    pendingConvId,
    loadConversations,
    loadMessages,
    create,
    remove,
    reset,
  }
})
