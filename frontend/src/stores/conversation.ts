import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import * as projectsApi from '../api/projects'
import type { Conversation, Message } from '../types'

export interface PastSession {
  conv: Conversation
  collapsed: boolean
  messages: Message[]
  loading: boolean
}

export const useConversationStore = defineStore('conversation', () => {
  const conversations = ref<Conversation[]>([])       // 当前项目所有会话(倒序,最新在前)
  const currentConvId = ref<number | null>(null)       // 当前活跃会话(最新)
  const messages = ref<Message[]>([])                  // 当前会话消息(完整显示)
  const pastSessions = ref<PastSession[]>([])          // 更早会话(折叠卡片,按需展开)
  const loading = ref(false)
  const loadingMore = ref(false)                       // 上翻加载更早会话中
  const loadedPastCount = ref(0)                       // 已加载到第几个历史会话
  const pendingConvId = ref<number | null>(null)
  const creating = ref(false)  // 防并发创建

  /** 当前会话标题 */
  const currentTitle = computed(() =>
    conversations.value[0]?.title || '新对话',
  )

  /** 切换项目: 加载会话列表 + 恢复或新建会话。 */
  async function loadConversations(projectId: number) {
    loading.value = true
    try {
      conversations.value = await projectsApi.listConversations(projectId)
      pastSessions.value = []
      loadedPastCount.value = 0

      // 恢复会话: 优先用 sessionStorage 记忆的 convId
      const storedCid = sessionStorage.getItem('activeConv_' + projectId)
      let targetConv: Conversation | undefined
      if (storedCid) {
        targetConv = conversations.value.find(c => c.id === Number(storedCid))
      }
      if (!targetConv) targetConv = conversations.value[0]

      if (targetConv) {
        currentConvId.value = targetConv.id
        const c = await projectsApi.getConversation(targetConv.id)
        messages.value = c.messages || []
      } else {
        currentConvId.value = null
        messages.value = []
      }

      // 加载历史会话卡片
      await loadMoreHistory()
    } finally {
      loading.value = false
    }
  }

  /** 上翻加载更早会话(每次 5 屏,只加载会话元数据,点击展开后再加载消息)。 */
  async function loadMoreHistory(): Promise<boolean> {
    const batch = 5
    const start = loadedPastCount.value + 1
    const end = Math.min(start + batch - 1, conversations.value.length - 1)
    if (start > end) return false
    loadingMore.value = true
    try {
      for (let i = start; i <= end; i++) {
        const conv = conversations.value[i]
        // 加载历史会话的消息(与当前会话同接口, 保证格式统一)
        let msgs: Message[] = []
        try {
          const c = await projectsApi.getConversation(conv.id)
          msgs = c.messages || []
        } catch { /* 忽略单条加载失败 */ }
        pastSessions.value.push({
          conv,
          collapsed: true,
          messages: msgs,
          loading: false,
        })
      }
      loadedPastCount.value = end
      return end < conversations.value.length - 1
    } finally {
      loadingMore.value = false
    }
  }

  /** 展开/折叠某个历史会话。 */
  async function togglePast(idx: number) {
    const s = pastSessions.value[idx]
    if (!s) return
    if (!s.collapsed) {
      s.collapsed = true
      return
    }
    // 展开 → 首次加载消息
    if (s.messages.length === 0) {
      s.loading = true
      try {
        const c = await projectsApi.getConversation(s.conv.id)
        s.messages = c.messages || []
      } finally {
        s.loading = false
      }
    }
    s.collapsed = false
  }

  /** 新建会话(当前项目下)。 */
  async function create(projectId: number, title?: string): Promise<Conversation> {
    creating.value = true
    try {
      const c = await projectsApi.createConversation(projectId, title)
      if (!conversations.value.some(x => x.id === c.id)) {
        conversations.value.unshift(c)
      }
      if (currentConvId.value && messages.value.length > 0) {
        const oldConv = conversations.value.find(x => x.id === currentConvId.value)
        if (oldConv) {
          pastSessions.value.unshift({
            conv: { ...oldConv }, collapsed: true,
            messages: [...messages.value], loading: false,
          })
        }
      }
      currentConvId.value = c.id
      messages.value = []
      sessionStorage.setItem('activeConv_' + projectId, String(c.id))
      return c
    } finally {
      creating.value = false
    }
  }

  function reset() {
    conversations.value = []
    currentConvId.value = null
    messages.value = []
    pastSessions.value = []
    loadedPastCount.value = 0
  }

  return {
    conversations, currentConvId, messages, pastSessions, loading, loadingMore,
    currentTitle, loadedPastCount, pendingConvId, creating,
    loadConversations, loadMoreHistory, togglePast, create, reset,
  }
})
