import { ref, type Ref } from 'vue'
import type { ToolReportMeta } from '@/components/AnalysisTimeline.vue'
import type { AnalysisStep } from './useTraceTimeline'

export interface AiStep {
  eventType: string
  description: string
  detail: string
  state: 'running' | 'completed' | 'failed'
  timestamp: number
  toolId?: string
}

export interface ItemEntry {
  type: 'text' | 'step' | 'tool' | 'sql-card' | 'media'
  text?: string
  // think/中间过程文本置灰，最终答复（RequestCompleted）恢复正常颜色
  interim?: boolean
  step?: AiStep
  toolResult?: { name: string; detail: string }
  // sql-card：与分析链路 step.id 关联，点击可跳转定位
  sqlCard?: { stepId: string; snippet: string; rowCount?: number; durationMs?: number; success: boolean }
  // media：工具产物联动（CHART=ECharts 交互渲染；IMAGE=普通图片；REPORT=报告查看/下载）
  media?: {
    images: string[]
    report: ToolReportMeta | null
    messageType?: 'TEXT' | 'CHART' | 'IMAGE' | 'REPORT'
    chartOption?: Record<string, any> | null
  }
}

export interface MessageItem {
  type: 'user' | 'ai'
  content: string
  loading?: boolean
  // 生成失败态（Error 事件/请求异常）：渲染时给出醒目样式
  failed?: boolean
  createTime?: string
  steps?: AiStep[]
  items?: ItemEntry[]
}

// SQL 卡片摘要截断长度
const SQL_SNIPPET_LEN = 80

/**
 * 对话消息列表状态与流式渲染操作（items/step/sql-card/media 的增改）。
 * activeGenerationMessageIndex 由页面创建并传入，与流处理域共享"当前生成中消息"的定位。
 */
export function useChatMessages(activeGenerationMessageIndex: Ref<number | null>) {
  const messages = ref<MessageItem[]>([])
  const messagesContainer = ref<HTMLElement>()

  const scrollToBottom = () => { if (messagesContainer.value) { messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight } }

  const getMessageAt = (index: number) => messages.value[index]

  const applyMessageChunk = (chunk: unknown) => {
    if (chunk === undefined || chunk === null) return
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage) return
    targetMessage.content = `${targetMessage.content || ''}${String(chunk)}`
    if (!targetMessage.items) targetMessage.items = []
    const last = targetMessage.items[targetMessage.items.length - 1]
    if (last && last.type === 'text') {
      last.text = (last.text || '') + String(chunk)
    } else {
      // 新文本段：生成中先置灰（think/中间过程），RequestCompleted 后恢复正文颜色
      targetMessage.items.push({ type: 'text', text: String(chunk), interim: true })
    }
    targetMessage.loading = false
    scrollToBottom()
  }

  const appendAiStep = (step: AiStep) => {
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage) return
    if (!targetMessage.steps) targetMessage.steps = []
    targetMessage.steps.push(step)
    if (!targetMessage.items) targetMessage.items = []
    targetMessage.items.push({ type: 'step', step })
    scrollToBottom()
  }

  const updateLastRunningToolStep = (detail: string, state: AiStep['state'], toolId?: string) => {
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage?.steps) return
    const steps = targetMessage.steps
    for (let i = steps.length - 1; i >= 0; i--) {
      const s = steps[i]
      if (!s) continue
      if (s.eventType === 'ToolExecutionStart' && s.state === 'running') {
        if (toolId && s.toolId && s.toolId !== toolId) continue
        s.state = state
        s.detail = detail
        scrollToBottom()
        return
      }
    }
  }

  const failAllRunningToolSteps = () => {
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage?.steps) return
    for (const step of targetMessage.steps) {
      if (step.eventType === 'ToolExecutionStart' && step.state === 'running') {
        step.state = 'failed'
        step.detail = '执行中断'
      }
    }
  }

  // 工具产物联动：成功且有图表/图片/报告时往当前消息 items 追加媒体卡片
  const appendMediaItem = (
    images: string[],
    report: ToolReportMeta | null,
    messageType: 'TEXT' | 'CHART' | 'IMAGE' | 'REPORT' = 'TEXT',
    chartOption: Record<string, any> | null = null,
  ) => {
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage) return
    if (!targetMessage.items) targetMessage.items = []
    targetMessage.items.push({ type: 'media', media: { images, report, messageType, chartOption } })
    scrollToBottom()
  }

  // 追加 SQL 卡片到当前生成中的 AI 消息 items 末尾，按时间穿插在 text/step/tool 之间
  const appendSqlCardItem = (step: AnalysisStep) => {
    const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage) return
    if (!targetMessage.items) targetMessage.items = []
    const sql = step.sql || ''
    const snippet = sql.length > SQL_SNIPPET_LEN ? sql.slice(0, SQL_SNIPPET_LEN) + '…' : sql
    targetMessage.items.push({
      type: 'sql-card',
      sqlCard: {
        stepId: step.id,
        snippet,
        rowCount: step.rowCount,
        durationMs: step.durationMs,
        success: step.status === 'completed',
      },
    })
    scrollToBottom()
  }

  // 最终答复恢复正文颜色：最后一个非文本 item 之后的文本段视为最终答复
  const finalizeFinalAnswer = () => {
    const m = getMessageAt(activeGenerationMessageIndex.value ?? -1)
    if (!m?.items?.length) return
    let start = 0
    for (let i = m.items.length - 1; i >= 0; i--) {
      const it = m.items[i]
      if (it && it.type !== 'text') { start = i + 1; break }
    }
    for (let i = start; i < m.items.length; i++) {
      const it = m.items[i]
      if (it && it.type === 'text') it.interim = false
    }
  }

  // 生成失败收尾：保留已流式内容，在其后追加错误提示并标记失败态（index 缺省取当前生成中的消息）
  const markMessageError = (errorText: string, index?: number) => {
    const targetMessage = getMessageAt(index ?? activeGenerationMessageIndex.value ?? -1)
    if (!targetMessage) return
    const notice = `⚠️ 执行出错：${errorText}`
    targetMessage.failed = true
    targetMessage.loading = false
    // items 与 content 双写错误提示，保证两种渲染路径都能看到
    if (!targetMessage.items) targetMessage.items = []
    const last = targetMessage.items[targetMessage.items.length - 1]
    if (last && last.type === 'text') {
      last.text = `${last.text || ''}\n\n${notice}`
    } else {
      targetMessage.items.push({ type: 'text', text: notice })
    }
    targetMessage.content = targetMessage.content ? `${targetMessage.content}\n\n${notice}` : notice
    scrollToBottom()
  }

  const resetMessages = () => { messages.value = [] }

  return {
    messages,
    messagesContainer,
    scrollToBottom,
    getMessageAt,
    applyMessageChunk,
    appendAiStep,
    updateLastRunningToolStep,
    failAllRunningToolSteps,
    appendMediaItem,
    appendSqlCardItem,
    finalizeFinalAnswer,
    markMessageError,
    resetMessages,
  }
}
