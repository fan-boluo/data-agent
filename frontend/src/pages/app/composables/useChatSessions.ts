import { ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'
import type { MessageItem } from './useChatMessages'
import { createClientId, getAuthHeaders, getAppSessionStorageKey } from './chatHelpers'

export interface SessionListItem {
  session_id: string
  first_message: string
  create_time: string
}

export interface UseChatSessionsOptions {
  appId: Ref<string | undefined>
  messages: Ref<MessageItem[]>
  /** 新建会话后的联动清理（如清空任务看板） */
  onSessionCreated?: () => void
}

/**
 * 会话管理：sessionId 维护（localStorage 按项目持久化）、历史列表、切换加载。
 * sessionId 为共享状态，由时间线/流处理域经 ref 传入使用。
 */
export function useChatSessions(options: UseChatSessionsOptions) {
  const { appId, messages, onSessionCreated } = options

  const sessionId = ref('')
  const sessionHistoryVisible = ref(false)
  const loadingSessionHistory = ref(false)
  const sessionList = ref<SessionListItem[]>([])

  const clearChatSessionId = () => { sessionId.value = '' }

  const createNewSession = () => {
    if (!appId.value) return
    const storageKey = getAppSessionStorageKey(appId.value)
    localStorage.removeItem(storageKey)
    sessionId.value = createClientId()
    localStorage.setItem(storageKey, sessionId.value)
    messages.value = []
    onSessionCreated?.()
    message.info('已创建新会话')
  }

  const toggleSessionHistory = async () => {
    if (!appId.value) return
    if (sessionHistoryVisible.value) {
      sessionHistoryVisible.value = false
      return
    }
    sessionHistoryVisible.value = true
    loadingSessionHistory.value = true
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await fetch(`${baseURL}/api/ai/sessions/${appId.value}?limit=5`, {
        headers: { ...getAuthHeaders() },
      })
      if (res.ok) {
        const result = await res.json()
        sessionList.value = result.data || []
      }
    } catch (e) {
      console.error('获取会话历史失败', e)
    } finally {
      loadingSessionHistory.value = false
    }
  }

  /**
   * 将 API 返回的 JSONL 消息转换为前端 MessageItem[]。
   * tool 角色消息合并入其前的 assistant 消息 items 中。
   */
  const convertMessagesFromApi = (msgs: any[]): MessageItem[] => {
    const items: MessageItem[] = []
    for (const m of msgs) {
      if (m.role === 'user') {
        items.push({ type: 'user', content: m.content || '', createTime: m.create_time })
      } else if (m.role === 'assistant') {
        if (m.content) {
          items.push({ type: 'ai', content: m.content, createTime: m.create_time })
        }
      } else if (m.role === 'tool') {
        const toolName = m.name || 'tool'
        const shortContent = (m.content || '').length > 200
          ? (m.content || '').substring(0, 200) + '...'
          : (m.content || '')
        items.push({
          type: 'ai',
          content: '',
          createTime: m.create_time,
          items: [{
            type: 'tool',
            toolResult: { name: toolName, detail: shortContent },
          }],
        })
      }
    }
    return items
  }

  const loadSession = async (sid: string) => {
    if (!appId.value) return
    sessionHistoryVisible.value = false
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await fetch(`${baseURL}/api/ai/sessions/${appId.value}/${sid}/messages?limit=50`, {
        headers: { ...getAuthHeaders() },
      })
      if (!res.ok) throw new Error('load failed')
      const result = await res.json()
      const msgs: any[] = result.data || []

      messages.value = convertMessagesFromApi(msgs)

      // Update localStorage with loaded session id
      const storageKey = getAppSessionStorageKey(appId.value)
      localStorage.setItem(storageKey, sid)
      sessionId.value = sid
      message.success('已加载历史会话')
    } catch (e) {
      console.error('加载会话消息失败', e)
      message.error('加载会话消息失败')
    }
  }

  const checkSessionAlive = async (sid: string): Promise<boolean> => {
    if (!appId.value) return false
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const res = await fetch(`${baseURL}/api/ai/sessions/${appId.value}/${sid}/alive`, {
        headers: { ...getAuthHeaders() },
      })
      if (!res.ok) return false
      const result = await res.json()
      return result.data?.alive === true
    } catch {
      return false
    }
  }

  const ensureChatSessionId = (currentAppId?: string) => {
    if (!currentAppId) { clearChatSessionId(); return '' }
    const storageKey = getAppSessionStorageKey(currentAppId)
    const storedSessionId = localStorage.getItem(storageKey)?.trim()
    if (storedSessionId) { sessionId.value = storedSessionId; return storedSessionId }
    const createdSessionId = createClientId()
    localStorage.setItem(storageKey, createdSessionId)
    sessionId.value = createdSessionId
    return createdSessionId
  }

  return {
    sessionId,
    sessionHistoryVisible,
    loadingSessionHistory,
    sessionList,
    clearChatSessionId,
    createNewSession,
    toggleSessionHistory,
    loadSession,
    checkSessionAlive,
    ensureChatSessionId,
  }
}
