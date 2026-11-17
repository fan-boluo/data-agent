import { nextTick, ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import { useOpenedChatsStore } from '@/stores/openedChats'
import request from '@/request'
import { API_BASE_URL } from '@/config/env'
import type { ElementInfo } from '@/utils/visualEditor'
import type { MessageItem, useChatMessages } from './useChatMessages'
import type { useTraceTimeline } from './useTraceTimeline'
import { createClientId, getAuthHeaders } from './chatHelpers'

/** 网页元素选择域（sendMessage 组装选中元素上下文用） */
export interface StreamElementSelection {
  isEditMode: Ref<boolean>
  selectedElementInfo: Ref<ElementInfo | null>
  clearSelectedElement: () => void
  toggleEditMode: () => void
}

export interface UseChatStreamOptions {
  appId: Ref<string | undefined>
  appInfo: Ref<API.AppVO>
  currentDb: Ref<string | undefined>
  messages: Ref<MessageItem[]>
  userInput: Ref<string>
  // 当前生成中的 AI 消息下标（页面创建，与消息域共享）
  activeGenerationMessageIndex: Ref<number | null>
  canOperateApp: Ref<boolean>
  /** 时间线域（useTraceTimeline 返回值） */
  timeline: ReturnType<typeof useTraceTimeline>
  /** 消息域（useChatMessages 返回值） */
  messagesApi: ReturnType<typeof useChatMessages>
  /** 看板任务数据推进（ToolExecutionEnd.task_data） */
  applyTaskData: (td: any) => void
  /** HITL 审批域 */
  showApproval: (eventData: any) => void
  clearApproval: () => void
  ensureChatSessionId: (currentAppId?: string) => string
  /** 生成结束后按需刷新源码树（有文件改动时） */
  reloadSourceTree: () => Promise<void>
  elementSelection: StreamElementSelection
  /** Log_Chunk / Terminal_Output 写入输出面板 */
  appendOutput: (type: 'stdout' | 'stderr' | 'system', text: string) => void
}

// task_create/task_update 的 render 通常是任务 JSON：压缩为"主题 · status · #id"一行摘要，避免时间线里堆原始 JSON
const summarizeTaskRender = (raw: unknown): string => {
  const text = String(raw ?? '')
  if (!text) return ''
  try {
    const obj = typeof raw === 'object' ? raw : JSON.parse(text)
    if (obj && typeof obj === 'object') {
      const o = obj as { subject?: string; title?: string; status?: string; id?: number }
      const parts = [o.subject || o.title || '', o.status ? `status=${o.status}` : '', o.id ? `#${o.id}` : ''].filter(Boolean)
      if (parts.length) return parts.join(' · ')
    }
  } catch { /* 非 JSON，原样返回 */ }
  return text
}

/**
 * SSE 流处理与事件接线：发送消息、建立 /api/ai/chat/gen 流、按事件类型分发到
 * 消息域/时间线域/看板域/审批域/输出面板，并管理生成态与停止请求。
 */
export function useChatStream(options: UseChatStreamOptions) {
  const {
    appId, appInfo, currentDb, messages, userInput,
    activeGenerationMessageIndex, canOperateApp, timeline, messagesApi,
    applyTaskData, showApproval, clearApproval, ensureChatSessionId,
    reloadSourceTree, elementSelection, appendOutput,
  } = options
  const {
    applyMessageChunk, appendAiStep, updateLastRunningToolStep, failAllRunningToolSteps,
    appendMediaItem, appendSqlCardItem, finalizeFinalAnswer, getMessageAt, scrollToBottom,
    markMessageError,
  } = messagesApi

  const openedChatsStore = useOpenedChatsStore()

  // 生成态
  const isGenerating = ref(false)
  const isStoppingGeneration = ref(false)
  const activeGenerationRequestId = ref('')
  const activeGenerationSessionId = ref('')
  const stopRequested = ref(false)
  const abortController = ref<AbortController | null>(null)

  // Track file-modifying tools for intelligent tree refresh
  const hadFileChangeInGeneration = ref(false)

  const clearActiveGeneration = (requestId?: string) => {
    if (requestId && activeGenerationRequestId.value && activeGenerationRequestId.value !== requestId) return
    activeGenerationRequestId.value = ''
    activeGenerationSessionId.value = ''
    activeGenerationMessageIndex.value = null
    stopRequested.value = false
    isStoppingGeneration.value = false
    timeline.clearGenerationAnchors()
  }

  const finishStream = () => {
    clearApproval()
    isGenerating.value = false
    isStoppingGeneration.value = false
    stopRequested.value = false
    // 时间线：本轮收尾（Error 已先行标 failed，此处不再覆盖）
    timeline.finishTraceTurn()
    if (appId.value) openedChatsStore.setGenerating(appId.value, false)
    clearActiveGeneration(activeGenerationRequestId.value)
    setTimeout(async () => { await refreshAfterGeneration() }, 1000)
  }

  const refreshAfterGeneration = async () => {
    if (hadFileChangeInGeneration.value) {
      hadFileChangeInGeneration.value = false
      await reloadSourceTree()
    }
  }

  const handleStreamEvent = (event: { event_type: string; data: any; state?: string; message_type?: string }) => {
    const eventType = event.event_type
    const eventData = event.data

    // ---- HITL：高风险工具执行前挂起，弹窗等待人工确认（同一 SSE 流保持连接）----
    if (eventType === 'ApprovalRequired') {
      showApproval(eventData)
      scrollToBottom()
      return
    }

    // ---- 计划任务推进（PlanStepChanged）：data 携带 task_id/status/subject，更新时间线任务节点 ----
    if (eventType === 'PlanStepChanged') {
      const planTaskId = Number(eventData?.task_id ?? eventData?.taskId ?? 0)
      if (planTaskId) {
        timeline.upsertTaskNode({
          id: planTaskId,
          subject: eventData?.subject || eventData?.title || undefined,
          status: eventData?.status || undefined,
        })
      }
      return
    }

    // ---- 分析链路专用事件（推右栏 trace，不混入 messages）----
    if (eventType === 'SQL_Start') {
      timeline.addTraceStep({ type: 'sql', title: 'SQL 查询', sql: eventData?.sql || '', durationMs: undefined })
      return
    }
    if (eventType === 'SQL_End') {
      const step = timeline.updateLastRunningTraceStep('sql', {
        sql: eventData?.sql,
        rowCount: eventData?.row_count ?? eventData?.rowCount,
        durationMs: eventData?.duration_ms ?? eventData?.durationMs,
        rowsPreview: eventData?.rows_preview || [],
        status: (eventData?.state === 'success') ? 'completed' : 'failed',
        error: eventData?.error || undefined,
      })
      // 同步往当前 AI 消息的 items 推一张 SQL 卡片（点击可跳到分析链路对应步骤）
      if (step) appendSqlCardItem(step)
      return
    }
    if (eventType === 'Chart_Start') {
      timeline.addTraceStep({ type: 'chart', title: eventData?.title || '生成可视化', chartType: eventData?.chart_type || '' })
      return
    }
    if (eventType === 'Chart_End') {
      timeline.updateLastRunningTraceStep('chart', {
        chartOption: eventData?.chart_option || eventData?.option,
        chartType: eventData?.chart_type,
        title: eventData?.title || '可视化',
        status: (eventData?.state === 'success') ? 'completed' : 'failed',
        error: eventData?.error || undefined,
      })
      return
    }
    if (eventType === 'Report_Start') {
      timeline.addTraceStep({ type: 'report', title: eventData?.title || '生成分析报告', markdown: '' })
      return
    }
    if (eventType === 'Report_End') {
      timeline.updateLastRunningTraceStep('report', {
        title: eventData?.title || '分析报告',
        markdown: eventData?.markdown || eventData?.content,
        status: (eventData?.state === 'success') ? 'completed' : 'failed',
        error: eventData?.error || undefined,
      })
      return
    }

    if (eventType === 'LLM_Response_Chunk') { applyMessageChunk(eventData); return }
    if (eventType === 'OnTurnStart') {
      appendAiStep({ eventType, description: '开始处理', detail: '', state: 'completed', timestamp: Date.now() })
      return
    }
    if (eventType === 'LLM_Thinking_Start') {
      const promptTokens = eventData?.prompt_tokens ?? 0
      const messageCount = eventData?.message_count ?? 0
      appendAiStep({ eventType, description: 'AI 思考中', detail: `${promptTokens} tokens, ${messageCount} 条消息`, state: 'completed', timestamp: Date.now() })
      // 时间线：think 节点（置灰展示）
      timeline.addThinkNode(`AI 思考中 · ${promptTokens} tokens · ${messageCount} 条消息`)
      return
    }
    if (eventType === 'ToolExecutionStart') {
      const toolName = eventData?.tool_name || eventData?.name || eventData?.function?.name || 'unknown'
      appendAiStep({ eventType, description: `${toolName}`, detail: '', state: 'running', timestamp: Date.now(), toolId: eventData?.tool_id || '' })
      // 时间线：新增工具节点（参数摘要）；事件携带 task_id 时归到对应计划任务
      timeline.addToolNode(String(eventData?.tool_id || ''), String(toolName), eventData?.arguments, Number(eventData?.task_id ?? 0) || undefined)
      return
    }
    if (eventType === 'ToolExecutionEnd') {
      const toolName = eventData?.tool_name || eventData?.name || 'unknown'
      const description = eventData?.description || ''
      const toolState = eventData?.state || ''
      const isSuccess = toolState === 'success'
      const detail = isSuccess ? description : (description || `失败: ${toolName}`)
      const toolId = eventData?.tool_id || ''
      // P0-4 展示元数据：一句话摘要 / 交互式图表（ECharts option）/ 图片 / 报告
      const isTaskTool = toolName === 'task_create' || toolName === 'task_update'
      const images: string[] = Array.isArray(eventData?.images) ? eventData.images : []
      const report = timeline.normalizeReport(eventData?.report)
      const chartOption = eventData?.chart_option || null
      // 更新任务面板 + 时间线任务节点
      if (eventData?.task_data) {
        applyTaskData(eventData.task_data)
      }
      const FILE_MODIFY_TOOLS = new Set(['write_file', 'edit_file', 'delete_file', 'bash', 'subagent'])
      if (isSuccess && FILE_MODIFY_TOOLS.has(toolName)) {
        hadFileChangeInGeneration.value = true
      }
      updateLastRunningToolStep(detail, isSuccess ? 'completed' : 'failed', toolId)
      // 时间线：回填工具节点状态/耗时/展示元数据（task 工具 render 压成一行摘要，其余原样保留）
      timeline.completeToolNode(String(toolId), String(toolName), isSuccess, {
        render: isTaskTool ? summarizeTaskRender(eventData?.render) : String(eventData?.render || ''),
        images,
        report,
        chartOption,
        error: !isSuccess ? String(eventData?.error_class || description || '执行失败') : undefined,
      })
      // 图表/报告联动：成功且有产物时往对话里追加媒体卡片
      if (isSuccess && (chartOption || images.length || report)) {
        // 后端 AgentEvent.message_type 透传（TEXT/CHART/IMAGE/REPORT）；老版本后端未传时按 data 推断
        let mt = (event as any).message_type as string | undefined
        if (!mt) mt = report ? 'REPORT' : (chartOption ? 'CHART' : (images.length ? 'IMAGE' : 'TEXT'))
        appendMediaItem(images, report, mt as any, chartOption)
      }
      return
    }
    if (eventType === 'CompactEvent') {
      const tokensBefore = eventData?.tokens_before ?? 0
      const tokensAfter = eventData?.tokens_after ?? 0
      const removed = eventData?.messages_removed ?? 0
      appendAiStep({ eventType, description: '上下文压缩', detail: `tokens: ${tokensBefore} → ${tokensAfter}, 移除 ${removed} 条`, state: 'completed', timestamp: Date.now() })
      return
    }
    if (eventType === 'RequestCompleted' || eventType === 'RequestStopped') {
      // 最终答复段恢复正文颜色（think/中间过程保持置灰）
      finalizeFinalAnswer()
      if (stopRequested.value) {
        const targetMessage = getMessageAt(activeGenerationMessageIndex.value ?? -1)
        if (targetMessage && !targetMessage.content) { targetMessage.content = '已停止本次生成。' }
      }
      finishStream(); return
    }
    if (eventType === 'Error') {
      failAllRunningToolSteps()
      // 时间线：本轮标记失败
      timeline.markCurrentTurnFailed()
      // 保留已流式内容，在其后追加错误提示并标记失败态（不覆盖正文）
      const errText = typeof eventData === 'string' ? eventData : String(eventData?.message || eventData?.error || '执行出错')
      markMessageError(errText)
      message.error('执行出错，请稍后重试'); finishStream(); return
    }
    if (eventType === 'Log_Chunk' || eventType === 'Terminal_Output') {
      const text = typeof eventData === 'string' ? eventData : eventData?.text || JSON.stringify(eventData)
      appendOutput('stdout', text)
      return
    }
    console.debug('收到未处理的事件:', eventType, eventData)
  }

  const sendMessage = async () => {
    if (!canOperateApp.value || !userInput.value.trim() || isGenerating.value) return
    let outgoingMessage = userInput.value.trim()
    if (elementSelection.selectedElementInfo.value) {
      let elementContext = `\n\n选中元素信息：`
      if (elementSelection.selectedElementInfo.value.pagePath) { elementContext += `\n- 页面路径: ${elementSelection.selectedElementInfo.value.pagePath}` }
      elementContext += `\n- 标签: ${elementSelection.selectedElementInfo.value.tagName.toLowerCase()}\n- 选择器: ${elementSelection.selectedElementInfo.value.selector}`
      if (elementSelection.selectedElementInfo.value.textContent) { elementContext += `\n- 当前内容: ${elementSelection.selectedElementInfo.value.textContent.substring(0, 100)}` }
      outgoingMessage += elementContext
    }
    if (!appId.value) {
      message.info('请先创建项目再开始对话')
      return
    }
    userInput.value = ''
    messages.value.push({ type: 'user', content: outgoingMessage })
    if (elementSelection.selectedElementInfo.value) {
      elementSelection.clearSelectedElement()
      if (elementSelection.isEditMode.value) { elementSelection.toggleEditMode() }
    }
    const aiMessageIndex = messages.value.length
    messages.value.push({ type: 'ai', content: '', loading: true })
    await nextTick(); scrollToBottom()
    isGenerating.value = true
    await generateCode(outgoingMessage, aiMessageIndex)
  }

  const generateCode = async (userMessage: string, aiMessageIndex: number) => {
    const currentSessionId = ensureChatSessionId(appId.value)
    const requestId = createClientId()
    activeGenerationRequestId.value = requestId; activeGenerationSessionId.value = currentSessionId
    activeGenerationMessageIndex.value = aiMessageIndex
    stopRequested.value = false; isStoppingGeneration.value = false
    // 时间线：按本次请求新开一个 turn 分组
    timeline.startTraceTurn(requestId, userMessage)
    if (appId.value) openedChatsStore.setGenerating(appId.value, true)
    const controller = new AbortController(); abortController.value = controller
    try {
      const baseURL = request.defaults.baseURL || API_BASE_URL
      const url = `${baseURL}/api/ai/chat/gen`
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ appId: String(appId.value), message: userMessage, sessionId: currentSessionId, requestId, traceId: createClientId(), dbName: appInfo.value?.dbName || null, currentDb: currentDb.value || null }),
        signal: controller.signal,
      })
      if (!response.ok) { throw new Error(`HTTP ${response.status}`) }
      const reader = response.body?.getReader()
      if (!reader) { throw new Error('No response body') }
      const decoder = new TextDecoder(); let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n'); buffer = lines.pop() || ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed) continue
          try { const event = JSON.parse(trimmed); if (event.event_type) { handleStreamEvent(event) } } catch { /* skip */ }
        }
      }
      if (buffer.trim()) { try { const event = JSON.parse(buffer.trim()); if (event.event_type) { handleStreamEvent(event) } } catch { /* skip */ } }
      finishStream()
    } catch (error: any) {
      if (error.name === 'AbortError') { return }
      handleError(error, aiMessageIndex, requestId)
    } finally { if (abortController.value === controller) { abortController.value = null } }
  }

  const handleError = (error: unknown, aiMessageIndex: number, requestId?: string) => {
    console.error('执行失败：', error)
    // 保留已流式内容，在其后追加错误提示并标记失败态（不覆盖正文）
    const err = error as Error
    markMessageError(err?.message || '网络异常，请重试', aiMessageIndex)
    message.error('执行失败，请重试'); isGenerating.value = false
    if (appId.value) openedChatsStore.setGenerating(appId.value, false)
    clearActiveGeneration(requestId)
  }

  const stopGeneration = async () => {
    if (!isGenerating.value || isStoppingGeneration.value || !appId.value || !activeGenerationRequestId.value || !activeGenerationSessionId.value) return
    isStoppingGeneration.value = true; stopRequested.value = true
    try {
      abortController.value?.abort()
      const baseURL = request.defaults.baseURL || API_BASE_URL
      await fetch(`${baseURL}/api/ai/chat/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ appId: String(appId.value), sessionId: activeGenerationSessionId.value, requestId: activeGenerationRequestId.value, traceId: createClientId(), reason: 'user-stop' }),
      })
      message.info('已停止生成')
    } catch (error) {
      console.error('停止请求失败：', error)
    } finally {
      finishStream()
    }
  }

  return {
    isGenerating,
    isStoppingGeneration,
    abortController,
    sendMessage,
    stopGeneration,
  }
}
