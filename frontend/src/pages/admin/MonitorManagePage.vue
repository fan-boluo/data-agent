<template>
  <div id="monitorManagePage" class="page-shell">
    <div class="page-header">
      <div class="subtle-text">当前会话池概览与单会话 Trace 排障；运行态势与告警见 Grafana</div>
      <a-space>
        <a-button :loading="poolLoading || sessionLoading" @click="refreshAll">刷新</a-button>
        <a-button @click="openGrafana">打开 Grafana 面板</a-button>
      </a-space>
    </div>

    <a-row :gutter="16" class="summary-row">
      <a-col v-for="item in summaryCards" :key="item.label" :xs="24" :sm="12" :xl="6">
        <a-card>
          <a-statistic :title="item.label" :value="item.value" :suffix="item.suffix" />
        </a-card>
      </a-col>
    </a-row>

    <a-card class="panel-card">
      <template #title>会话检索</template>
      <a-form layout="inline" :model="sessionQuery" class="search-form">
        <a-form-item label="Session">
          <a-input v-model:value="sessionQuery.sessionId" placeholder="输入 sessionId" allow-clear />
        </a-form-item>
        <a-form-item label="用户">
          <a-input v-model:value="sessionQuery.userId" placeholder="输入 userId" allow-clear />
        </a-form-item>
        <a-form-item label="应用">
          <a-input v-model:value="sessionQuery.appId" placeholder="输入 appId" allow-clear />
        </a-form-item>
        <a-form-item label="状态">
          <a-select v-model:value="sessionQuery.status" allow-clear style="width: 140px" placeholder="全部状态">
            <a-select-option value="running">运行中</a-select-option>
            <a-select-option value="idle">空闲</a-select-option>
            <a-select-option value="closed">已关闭</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item>
          <a-space>
            <a-button type="primary" @click="doSessionSearch">搜索</a-button>
            <a-button @click="resetSessionSearch">重置</a-button>
          </a-space>
        </a-form-item>
      </a-form>
      <a-divider />
      <a-table
        row-key="sessionId"
        :loading="sessionLoading"
        :columns="sessionColumns"
        :data-source="sessionPage.records"
        :pagination="sessionPagination"
        :scroll="{ x: 1000 }"
        @change="onSessionTableChange"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'status'">
            <a-tag :color="poolStatusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'lastActivityAt'">
            {{ formatRelativeTime(record.lastActivityAt) }}
          </template>
          <template v-else-if="column.dataIndex === 'idleSeconds'">
            {{ record.inPool ? formatIdle(record.idleSeconds) : '-' }}
          </template>
          <template v-else-if="column.dataIndex === 'action'">
            <a-button type="link" size="small" @click="openSessionDetail(record)">查看详情</a-button>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-drawer v-model:open="sessionDrawerOpen" title="Session 详情" width="760" destroy-on-close>
      <template v-if="selectedPoolSession">
        <a-descriptions bordered :column="2" size="small">
          <a-descriptions-item label="Session ID" :span="2">{{ selectedPoolSession.sessionId }}</a-descriptions-item>
          <a-descriptions-item label="池状态">
            <a-tag :color="poolStatusColor(selectedPoolSession.status)">{{ statusLabel(selectedPoolSession.status) }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="应用">{{ selectedPoolSession.appId || '-' }}</a-descriptions-item>
          <a-descriptions-item label="用户">{{ selectedPoolSession.userId || '-' }}</a-descriptions-item>
          <a-descriptions-item label="轮次">{{ selectedPoolSession.totalTurns }}</a-descriptions-item>
          <a-descriptions-item label="最后活跃">{{ formatRelativeTime(selectedPoolSession.lastActivityAt) }}</a-descriptions-item>
          <a-descriptions-item label="待处理请求">
            {{ selectedPoolSession.inPool ? selectedPoolSession.pendingRequests : '-' }}
          </a-descriptions-item>
        </a-descriptions>

        <a-divider>Turn 链路</a-divider>
        <a-spin :spinning="turnLoading">
          <a-table
            v-if="sessionTurns.length"
            row-key="turnId"
            size="small"
            :columns="turnColumns"
            :data-source="sessionTurns"
            :pagination="false"
            :scroll="{ x: 720 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.dataIndex === 'status'">
                <a-tag :color="turnStatusColor(record.status)">{{ record.status }}</a-tag>
              </template>
              <template v-else-if="column.dataIndex === 'durationMs'">{{ formatMs(record.durationMs) }}</template>
              <template v-else-if="column.dataIndex === 'traceId'">
                <span class="mono-text" :title="record.traceId">{{ shortTraceId(record.traceId) }}</span>
              </template>
              <template v-else-if="column.dataIndex === 'action'">
                <a-button type="link" size="small" @click="openTurnSpans(record)">查看链路</a-button>
              </template>
            </template>
          </a-table>
          <a-empty v-else description="该会话暂无 Turn 记录（可能尚未产生可观测数据）" />
        </a-spin>
      </template>
    </a-drawer>

    <a-drawer v-model:open="spanDrawerOpen" title="Turn 链路" width="900" destroy-on-close>
      <template v-if="currentTurn">
        <a-descriptions bordered :column="3" size="small">
          <a-descriptions-item label="Trace ID" :span="3">
            <span class="mono-text">{{ currentTurn.traceId || '-' }}</span>
          </a-descriptions-item>
          <a-descriptions-item label="状态">
            <a-tag :color="turnStatusColor(currentTurn.status)">{{ currentTurn.status }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="Turn 耗时">{{ formatMs(currentTurn.durationMs) }}</a-descriptions-item>
          <a-descriptions-item label="错误数">{{ currentTurn.errorCount }}</a-descriptions-item>
          <a-descriptions-item label="LLM 总耗时">{{ formatMs(currentTurn.llmLatencyMs) }}</a-descriptions-item>
          <a-descriptions-item label="首包耗时">{{ formatMs(currentTurn.firstTokenMs) }}</a-descriptions-item>
          <a-descriptions-item label="Span 数">{{ spanTree?.spanCount ?? 0 }}</a-descriptions-item>
        </a-descriptions>

        <a-divider>Span 瀑布</a-divider>
        <a-spin :spinning="spanLoading">
          <div v-if="spanRows.length" class="span-list">
            <div
              v-for="row in spanRows"
              :key="row.spanId"
              class="span-row"
              :style="{ paddingLeft: `${row.depth * 18}px` }"
            >
              <div class="span-head">
                <a-tag class="span-kind-tag" :color="spanKindMeta(row.opType).tagColor">
                  {{ spanKindMeta(row.opType).label }}
                </a-tag>
                <span class="span-name">{{ row.name || row.opType }}</span>
                <a-tag :color="row.status === 'error' ? 'red' : 'green'">{{ row.status }}</a-tag>
                <span class="span-meta">{{ row.opType }} · {{ formatMs(row.durationMs) }}</span>
              </div>
              <div class="span-track">
                <div
                  class="span-bar"
                  :style="{
                    marginLeft: `${row.offsetPercent}%`,
                    width: `${row.widthPercent}%`,
                    background: row.status === 'error' ? '#ff4d4f' : spanKindMeta(row.opType).barColor,
                  }"
                />
              </div>
              <div v-if="row.error" class="span-error">{{ row.error }}</div>
            </div>
          </div>
          <a-empty v-else-if="!spanLoading" description="暂无 Span 数据" />
        </a-spin>
      </template>
    </a-drawer>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  getMonitorSessionDetail,
  getMonitorTurnSpans,
  getSessionPool,
  listAllSessions,
} from '@/api/monitorController'
import type {
  MergedSessionRow,
  MonitorPageData,
  MonitorSpanNode,
  MonitorTurnSpanTree,
  MonitorTurnSummary,
  PoolSessionSummary,
  SessionPoolStats,
} from '@/types/monitor'

const poolLoading = ref(false)
const sessionLoading = ref(false)
const turnLoading = ref(false)
const spanLoading = ref(false)

const poolStats = ref<SessionPoolStats>()
// 池内快照仅用于概览卡片统计，列表数据来自后端合并接口
const poolSessions = ref<PoolSessionSummary[]>([])
const selectedPoolSession = ref<MergedSessionRow>()
const sessionTurns = ref<MonitorTurnSummary[]>([])

const sessionPage = ref<MonitorPageData<MergedSessionRow>>({
  records: [],
  pageNumber: 1,
  pageSize: 10,
  totalPage: 0,
  totalRow: 0,
})
const sessionPageNumber = ref(1)
const sessionPageSize = ref(10)

const sessionDrawerOpen = ref(false)
const spanDrawerOpen = ref(false)
const currentTurn = ref<MonitorTurnSummary>()
const spanTree = ref<MonitorTurnSpanTree>()

const sessionQuery = reactive({
  sessionId: '',
  userId: '',
  appId: '',
  status: undefined as string | undefined,
})

const sessionColumns = [
  { title: 'Session ID', dataIndex: 'sessionId', width: 260 },
  { title: '应用', dataIndex: 'appId', width: 120 },
  { title: '用户', dataIndex: 'userId', width: 120 },
  { title: '状态', dataIndex: 'status', width: 100 },
  { title: '轮次', dataIndex: 'totalTurns', width: 80 },
  { title: '空闲时长', dataIndex: 'idleSeconds', width: 110 },
  { title: '最后活跃', dataIndex: 'lastActivityAt', width: 140 },
  { title: '操作', dataIndex: 'action', width: 100, fixed: 'right' as const },
]

const turnColumns = [
  { title: '轮次', dataIndex: 'turnNumber', width: 70 },
  { title: '状态', dataIndex: 'status', width: 90 },
  { title: '耗时', dataIndex: 'durationMs', width: 100 },
  { title: '错误数', dataIndex: 'errorCount', width: 80 },
  { title: 'Trace ID', dataIndex: 'traceId', width: 200 },
  { title: '操作', dataIndex: 'action', width: 100, fixed: 'right' as const },
]

// 服务端分页：页码/每页大小由后端合并接口返回
const sessionPagination = computed(() => ({
  current: sessionPage.value.pageNumber,
  pageSize: sessionPage.value.pageSize,
  total: sessionPage.value.totalRow,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50'],
  showTotal: (total: number) => `共 ${total} 条`,
}))

// 概览只反映会话池当前状态，趋势类指标统一看 Grafana
const summaryCards = computed(() => [
  {
    label: '当前会话',
    value: poolStats.value?.totalSessions ?? 0,
    suffix: ` / ${poolStats.value?.maxSessions ?? 0}`,
  },
  { label: '运行中', value: poolStats.value?.runningSessions ?? 0 },
  { label: '空闲', value: poolStats.value?.idleSessions ?? 0 },
  {
    label: '待处理请求',
    value: poolSessions.value.reduce((sum, item) => sum + item.pendingRequests, 0),
  },
])

// 扁平 span 按父节点计算缩进层级，并按开始偏移量还原时间顺序
const spanRows = computed(() => {
  const tree = spanTree.value
  if (!tree?.spans?.length) return []
  const byId = new Map(tree.spans.map((span) => [span.spanId, span]))
  const depthOf = (node: MonitorSpanNode, guard = 0): number => {
    if (!node.parentSpanId || guard > 20) return 0
    const parent = byId.get(node.parentSpanId)
    return parent ? depthOf(parent, guard + 1) + 1 : 0
  }
  const total =
    tree.totalDurationMs || Math.max(...tree.spans.map((span) => span.offsetMs + span.durationMs), 1)
  return [...tree.spans]
    .sort((a, b) => a.offsetMs - b.offsetMs || a.seq - b.seq)
    .map((span) => {
      const offsetPercent = Math.min(100, Math.max(0, (span.offsetMs / total) * 100))
      return {
        ...span,
        depth: depthOf(span),
        offsetPercent,
        widthPercent: Math.max(0.5, Math.min(100 - offsetPercent, (span.durationMs / total) * 100)),
      }
    })
})

const fetchPool = async () => {
  poolLoading.value = true
  try {
    const res = await getSessionPool()
    if (res.data.code === 0 && res.data.data) {
      poolStats.value = res.data.data.stats
      poolSessions.value = res.data.data.sessions || []
      return
    }
    message.error(`获取会话池失败：${res.data.message}`)
  } catch {
    message.error('获取会话池失败')
  } finally {
    poolLoading.value = false
  }
}

// 合并会话列表：后端把池内会话与历史会话合并，池外会话状态统一为 closed
const fetchSessions = async () => {
  sessionLoading.value = true
  try {
    const res = await listAllSessions({
      pageNum: sessionPageNumber.value,
      pageSize: sessionPageSize.value,
      sessionId: sessionQuery.sessionId || undefined,
      userId: sessionQuery.userId || undefined,
      appId: sessionQuery.appId || undefined,
      status: sessionQuery.status || undefined,
    })
    if (res.data.code === 0 && res.data.data) {
      sessionPage.value = res.data.data
      return
    }
    message.error(`获取会话列表失败：${res.data.message}`)
  } catch {
    message.error('获取会话列表失败')
  } finally {
    sessionLoading.value = false
  }
}

const refreshAll = () => Promise.all([fetchPool(), fetchSessions()])

const doSessionSearch = () => {
  sessionPageNumber.value = 1
  fetchSessions()
}

const resetSessionSearch = () => {
  sessionQuery.sessionId = ''
  sessionQuery.userId = ''
  sessionQuery.appId = ''
  sessionQuery.status = undefined
  sessionPageNumber.value = 1
  fetchSessions()
}

const onSessionTableChange = (pagination: { current?: number; pageSize?: number }) => {
  sessionPageNumber.value = pagination.current || 1
  sessionPageSize.value = pagination.pageSize || sessionPageSize.value
  fetchSessions()
}

const openSessionDetail = async (record: MergedSessionRow) => {
  selectedPoolSession.value = record
  sessionTurns.value = []
  sessionDrawerOpen.value = true
  turnLoading.value = true
  try {
    const res = await getMonitorSessionDetail(record.sessionId)
    if (res.data.code === 0 && res.data.data) {
      sessionTurns.value = res.data.data.turns || []
    }
  } catch {
    // 会话尚无 Trace 记录时后端可能直接报错，按空态处理
  } finally {
    turnLoading.value = false
  }
}

const openTurnSpans = async (turn: MonitorTurnSummary) => {
  if (!selectedPoolSession.value) return
  currentTurn.value = turn
  spanTree.value = undefined
  spanDrawerOpen.value = true
  spanLoading.value = true
  try {
    const res = await getMonitorTurnSpans(selectedPoolSession.value.sessionId, turn.turnId)
    if (res.data.code === 0 && res.data.data) {
      spanTree.value = res.data.data as MonitorTurnSpanTree
      return
    }
    message.error(`获取链路失败：${res.data.message}`)
  } catch {
    message.error('获取链路失败')
  } finally {
    spanLoading.value = false
  }
}

// 运行态势与告警统一在 Grafana 查看，前端只做业务排障
const openGrafana = () => {
  window.open(import.meta.env.VITE_GRAFANA_URL || 'http://127.0.0.1:3000', '_blank')
}

const statusLabel = (status?: string) =>
  ({ running: '运行中', idle: '空闲', closed: '已关闭' } as Record<string, string>)[status || ''] ||
  status ||
  '-'

const poolStatusColor = (status?: string) =>
  ({ running: 'blue', idle: 'green', closed: 'default' } as Record<string, string>)[status || ''] ||
  'default'

const turnStatusColor = (status?: string) =>
  ({ success: 'green', error: 'red', running: 'blue', stopped: 'orange' } as Record<string, string>)[
    status || ''
  ] || 'default'

// span 类别区分：标签色 + 时间条色（错误统一红色覆盖）
const spanKindMeta = (opType?: string) => {
  const map: Record<string, { label: string; tagColor: string; barColor: string }> = {
    agent: { label: 'Agent', tagColor: 'purple', barColor: '#722ed1' },
    tool: { label: '工具', tagColor: 'orange', barColor: '#fa8c16' },
    retrieval: { label: '检索', tagColor: 'gold', barColor: '#faad14' },
    llm: { label: 'LLM', tagColor: 'blue', barColor: '#1677ff' },
    compaction: { label: '压缩', tagColor: 'default', barColor: '#8c8c8c' },
    turn: { label: 'Turn', tagColor: 'geekblue', barColor: '#2f54eb' },
  }
  return map[opType || ''] || { label: opType || '其他', tagColor: 'default', barColor: '#8c8c8c' }
}

const formatMs = (value?: number) => (value == null ? '-' : `${Math.round(Number(value))} ms`)

const formatIdle = (seconds?: number) => {
  const value = Number(seconds || 0)
  if (value < 60) return `${value}s`
  if (value < 3600) return `${Math.floor(value / 60)}m`
  return `${(value / 3600).toFixed(1)}h`
}

const formatRelativeTime = (value?: string) => {
  if (!value) return '-'
  const ts = new Date(value).getTime()
  if (Number.isNaN(ts)) return value
  const diff = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (diff < 60) return `${diff} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return new Date(ts).toLocaleString()
}

const shortTraceId = (traceId?: string) => {
  if (!traceId) return '-'
  return traceId.length > 18 ? `${traceId.slice(0, 18)}…` : traceId
}

onMounted(() => {
  refreshAll()
})
</script>

<style scoped>
/* 页边距统一由全局 .page-shell 模板类提供（styles/theme.css） */

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.page-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 600;
}

.summary-row,
.panel-card {
  margin-bottom: 16px;
}

.search-form {
  row-gap: 8px;
}

.subtle-text {
  color: rgba(0, 0, 0, 0.45);
  font-size: 12px;
}

.mono-text {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.span-list {
  display: grid;
  gap: 12px;
}

.span-row {
  display: grid;
  gap: 4px;
}

.span-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.span-name {
  font-weight: 500;
}

.span-meta {
  color: rgba(0, 0, 0, 0.45);
  font-size: 12px;
}

.span-track {
  height: 8px;
  background: #f0f0f0;
  border-radius: 4px;
  overflow: hidden;
}

.span-bar {
  height: 100%;
  border-radius: 4px;
}

.span-kind-tag {
  min-width: 44px;
  text-align: center;
  margin-right: 0;
}

.span-error {
  color: #ff4d4f;
  font-size: 12px;
}
</style>
