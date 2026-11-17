import { computed, nextTick, ref, watch, type Ref } from 'vue'
import type {
  ToolReportMeta, TraceNode, TraceToolNode, TraceTaskNode, TraceTurn,
} from '@/components/AnalysisTimeline.vue'

// 分析链路步骤（右栏"分析链路"Tab 用，按 session 内存缓存，后端不保留）
export interface AnalysisStep {
  id: string
  type: 'sql' | 'chart' | 'report'
  title: string
  status: 'running' | 'completed' | 'failed'
  timestamp: number
  // sql 专属
  sql?: string
  rowCount?: number
  durationMs?: number
  rowsPreview?: Record<string, any>[]  // SQL 执行结果前 N 行（后端 extras.rows_preview）
  // chart 专属（echarts option）
  chartOption?: any
  chartType?: string
  // report 专属
  markdown?: string
  // 通用错误信息
  error?: string
}

// 记录用户手动拖宽前的默认宽度，切到 trace 自动加宽、切回恢复
const DEFAULT_CHAT_WIDTH = 320
const TRACE_CHAT_WIDTH = 480

/**
 * 分析链路时间线状态与节点增改（按 request/turn 组织：任务/工具/思考节点）。
 * 按 sessionId 内存缓存（页面刷新后清空，后端不持久化）；
 * chatWidth 由面板拖拽域传入，切到"分析链路"Tab 时自动加宽。
 */
export function useTraceTimeline(sessionId: Ref<string>, chatWidth: Ref<number>) {
  const analysisTraceMap = ref<Map<string, AnalysisStep[]>>(new Map())
  const activeAnalysisTrace = computed<AnalysisStep[]>(() => {
    const sid = sessionId.value
    if (!sid) return []
    let arr = analysisTraceMap.value.get(sid)
    if (!arr) {
      arr = []
      analysisTraceMap.value.set(sid, arr)
    }
    return arr
  })

  // 右栏 Tab 切换：对话 / 分析链路
  const chatTab = ref<'chat' | 'trace'>('chat')

  // 时间线：按 request/turn 组织（任务/工具/思考节点），按 session 内存缓存
  const traceTurnsMap = ref<Map<string, TraceTurn[]>>(new Map())
  // 当前生成中的 turn（存 reactive 代理，保证节点变更可触发渲染）
  const currentTraceTurn = ref<TraceTurn | null>(null)
  // 进行中的计划任务节点 id：后续工具调用归到该任务下（DAG 推进展示）
  const currentTaskNodeId = ref('')
  const activeTraceTurns = computed<TraceTurn[]>(() => {
    const sid = sessionId.value
    if (!sid) return []
    let arr = traceTurnsMap.value.get(sid)
    if (!arr) {
      arr = []
      traceTurnsMap.value.set(sid, arr)
    }
    return arr
  })

  let savedChatWidth = DEFAULT_CHAT_WIDTH

  // 切右栏 Tab 时自动调宽（只在用户没手动拖过时生效）
  watch(chatTab, (tab) => {
    if (tab === 'trace' && chatWidth.value <= DEFAULT_CHAT_WIDTH + 5) {
      savedChatWidth = chatWidth.value
      chatWidth.value = TRACE_CHAT_WIDTH
    } else if (tab === 'chat' && chatWidth.value === TRACE_CHAT_WIDTH) {
      chatWidth.value = savedChatWidth || DEFAULT_CHAT_WIDTH
    }
  })

  // session 切换时自动清空当前的 analysisTrace（切走再切回来页面已刷新则自然丢失）
  watch(sessionId, () => {
    // analysisTraceMap 里按 sessionId 已做了隔离，此处无需清理 Map
  })

  const genTraceStepId = () => `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

  // 新增一个 running/completed 的分析步骤
  const addTraceStep = (partial: Partial<AnalysisStep> & { type: AnalysisStep['type']; title: string }) => {
    // id 放在展开之后，保证必生成分配；type/title 由入参约束必填
    const step = {
      status: 'running',
      timestamp: Date.now(),
      ...partial,
      id: genTraceStepId(),
    } as AnalysisStep
    activeAnalysisTrace.value.push(step)
    // 同步挂到当前 turn 时间线（analysis 节点按 stepId 引用，状态更新走平面列表）
    if (currentTraceTurn.value) {
      currentTraceTurn.value.nodes.push({ kind: 'analysis', id: step.id, stepId: step.id })
    }
    return step
  }

  // 参数摘要：JSON 截断，时间线单行展示
  const summarizeArgs = (args: any): string => {
    if (!args || typeof args !== 'object') return ''
    try {
      const s = JSON.stringify(args)
      return s.length > 60 ? s.slice(0, 60) + '…' : s
    } catch { return '' }
  }

  // 报告元数据归一化：后端 file_name/created_at，前端统一 camelCase
  const normalizeReport = (raw: any): ToolReportMeta | null => {
    if (!raw || typeof raw !== 'object') return null
    return {
      title: raw.title || '',
      path: raw.path || '',
      fileName: raw.fileName || raw.file_name || '',
      markdown: raw.markdown || '',
      createdAt: raw.createdAt ?? raw.created_at,
    }
  }

  // 按 node id 查找节点（跨 turn，任务节点含子级）
  const findTimelineNodeById = (nodeId: string): TraceNode | undefined => {
    for (const turn of activeTraceTurns.value) {
      for (const node of turn.nodes) {
        if (node.id === nodeId) return node
        if (node.kind === 'task') {
          const child = node.children.find(c => c.id === nodeId)
          if (child) return child
        }
      }
    }
    return undefined
  }

  // 按 tool_id 查找工具节点（Start/End 事件关联）
  const findToolNode = (toolId: string): TraceToolNode | undefined => {
    for (const turn of activeTraceTurns.value) {
      for (const node of turn.nodes) {
        if (node.kind === 'tool' && node.toolId === toolId) return node
        if (node.kind === 'task') {
          const child = node.children.find(c => c.kind === 'tool' && c.toolId === toolId)
          if (child) return child as TraceToolNode
        }
      }
    }
    return undefined
  }

  // 跨 turn 查找计划任务节点（任务看板会话级，任务节点更新不限于当前 turn）
  const findTimelineTaskNode = (taskId: number): TraceTaskNode | undefined => {
    for (const turn of activeTraceTurns.value) {
      for (const node of turn.nodes) {
        if (node.kind === 'task' && node.taskId === taskId) return node
      }
    }
    return undefined
  }

  // 计划任务节点：task_data（task_create/task_update）与 PlanStepChanged 共用；deleted 移除
  const upsertTaskNode = (task: { id?: number; subject?: string; status?: string }) => {
    const taskId = Number(task?.id ?? 0)
    if (!taskId) return
    const existing = findTimelineTaskNode(taskId)
    const rawStatus = String(task.status || '')
    if (rawStatus === 'deleted') {
      if (existing) {
        // 已删除任务：从所属 turn 顶层移除节点
        for (const turn of activeTraceTurns.value) {
          const idx = turn.nodes.indexOf(existing)
          if (idx >= 0) { turn.nodes.splice(idx, 1); break }
        }
      }
      if (currentTaskNodeId.value === existing?.id) currentTaskNodeId.value = ''
      return
    }
    let node = existing
    if (!node) {
      node = { kind: 'task', id: genTraceStepId(), taskId, subject: task.subject || `任务 #${taskId}`, status: 'pending', children: [] }
      currentTraceTurn.value?.nodes.push(node)
    }
    if (task.subject) node.subject = task.subject
    if (rawStatus === 'pending' || rawStatus === 'in_progress' || rawStatus === 'completed' || rawStatus === 'failed') {
      node.status = rawStatus
    }
    // 进行中任务记为锚点：后续工具事件归到该任务下；完成后解除
    if (node.status === 'in_progress') currentTaskNodeId.value = node.id
    else if (currentTaskNodeId.value === node.id) currentTaskNodeId.value = ''
  }

  // 工具调用节点：事件 task_id 优先归到对应任务，否则挂进行中任务/turn 顶层；返回节点 id 供主对话卡片跳转定位
  const addToolNode = (toolId: string, name: string, args: any, taskId?: number): string => {
    const turn = currentTraceTurn.value
    if (!turn) return ''
    const node: TraceToolNode = {
      kind: 'tool', id: genTraceStepId(), toolId, name,
      argsSummary: summarizeArgs(args), args,
      status: 'running', startTs: Date.now(),
    }
    const anchor = (taskId ? findTimelineTaskNode(taskId) : undefined)
      ?? (currentTaskNodeId.value ? findTimelineNodeById(currentTaskNodeId.value) : undefined)
    if (anchor && anchor.kind === 'task') anchor.children.push(node)
    else turn.nodes.push(node)
    return node.id
  }

  // 工具执行结束：按 tool_id 回填状态/耗时/展示元数据（render/images/report，SQL 工具附卡片数据）；返回节点 id
  const completeToolNode = (
    toolId: string, name: string, success: boolean,
    meta: {
      render?: string
      images?: string[]
      report?: ToolReportMeta | null
      chartOption?: Record<string, any> | null
      error?: string
      sql?: string
      rowCount?: number
      rowsPreview?: Record<string, any>[]
    },
  ): string => {
    let node = toolId ? findToolNode(toolId) : undefined
    if (!node && currentTraceTurn.value) {
      // Start 事件丢失时兜底补一个已结束节点
      node = { kind: 'tool', id: genTraceStepId(), toolId, name, argsSummary: '', status: 'running', startTs: Date.now() }
      currentTraceTurn.value.nodes.push(node)
    }
    if (!node) return ''
    node.status = success ? 'completed' : 'failed'
    node.durationMs = Date.now() - node.startTs
    if (meta.render) node.render = meta.render
    if (meta.images?.length) node.images = meta.images
    if (meta.report) node.report = meta.report
    if (meta.chartOption) node.chartOption = meta.chartOption
    if (!success && meta.error) node.error = meta.error
    // SQL 卡片数据：ToolNodeItem 据此把工具节点渲染成 SQL 卡片（SQL + 结果预览）
    if (meta.sql) node.sql = meta.sql
    if (meta.rowCount != null) node.rowCount = meta.rowCount
    if (meta.rowsPreview) node.rowsPreview = meta.rowsPreview
    return node.id
  }

  // think 节点：思考阶段入时间线（置灰展示）
  const addThinkNode = (text: string) => {
    currentTraceTurn.value?.nodes.push({ kind: 'think', id: genTraceStepId(), text, timestamp: Date.now() })
  }

  // 新开一个 turn 分组（存回 reactive 数组取代理，保证后续变更可触发渲染）
  const startTraceTurn = (requestId: string, label: string) => {
    const turns = activeTraceTurns.value
    turns.push({ id: requestId, label: label.length > 40 ? label.slice(0, 40) + '…' : label, status: 'running', timestamp: Date.now(), nodes: [] })
    currentTraceTurn.value = turns[turns.length - 1] ?? null
  }

  // 更新最后一个 running 的指定 type 步骤（sql/chart/report 执行完更新状态和内容）
  const updateLastRunningTraceStep = (type: AnalysisStep['type'], patch: Partial<AnalysisStep>) => {
    const list = activeAnalysisTrace.value
    for (let i = list.length - 1; i >= 0; i--) {
      const cur = list[i]
      if (cur && cur.type === type && cur.status === 'running') {
        list[i] = { ...cur, ...patch, status: patch.status ?? 'completed' }
        return list[i]!
      }
    }
    return null
  }

  // 点击 SQL 卡片 → 切到分析链路 Tab 并滚动定位到对应步骤卡片
  const jumpToTraceStep = async (stepId?: string) => {
    if (!stepId) return
    chatTab.value = 'trace'
    await nextTick()
    const el = document.getElementById(`trace-step-${stepId}`)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  // 生成收尾：本轮 running turn 标记完成（Error 已先行标 failed，此处不再覆盖）
  const finishTraceTurn = () => {
    if (currentTraceTurn.value?.status === 'running') currentTraceTurn.value.status = 'completed'
  }

  // Error 事件：本轮标记失败
  const markCurrentTurnFailed = () => {
    if (currentTraceTurn.value) currentTraceTurn.value.status = 'failed'
  }

  // 清空生成锚点（生成结束后调用）
  const clearGenerationAnchors = () => {
    currentTraceTurn.value = null
    currentTaskNodeId.value = ''
  }

  return {
    chatTab,
    analysisTraceMap,
    activeAnalysisTrace,
    traceTurnsMap,
    currentTraceTurn,
    currentTaskNodeId,
    activeTraceTurns,
    addTraceStep,
    normalizeReport,
    upsertTaskNode,
    addToolNode,
    completeToolNode,
    addThinkNode,
    startTraceTurn,
    updateLastRunningTraceStep,
    jumpToTraceStep,
    finishTraceTurn,
    markCurrentTurnFailed,
    clearGenerationAnchors,
  }
}
