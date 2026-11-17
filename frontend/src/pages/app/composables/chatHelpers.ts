// 聊天页通用小工具：客户端 ID / 鉴权头 / 会话存储键（无状态，可跨 composable 复用）
export const createClientId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export const getAuthHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('token')
  if (!token) return {}
  return { Authorization: `Bearer ${token}` }
}

export const getAppSessionStorageKey = (currentAppId: string) => `codegenx:app-chat-session:${currentAppId}`
